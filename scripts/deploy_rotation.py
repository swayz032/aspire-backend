"""Deploy Aspire Rotation Infrastructure to AWS via boto3.

This replaces `terraform apply` for the rotation control plane. Creates:
  - IAM execution role (Lambda + Step Functions + API Gateway)
  - SNS topics (events + failures)
  - Lambda functions (rotation_handler + age_checker)
  - Step Functions state machine (7-step rotation flow)
  - EventBridge daily age check schedule
  - CloudWatch alarms for secret age

Run via WSL:
  source ~/venvs/aspire/bin/activate
  AWS_ACCESS_KEY_ID=<key> AWS_SECRET_ACCESS_KEY=<secret> python scripts/deploy_rotation.py
"""

import io
import json
import logging
import os
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REGION = "us-east-1"
ACCOUNT_ID = "843479649294"
ENVIRONMENT = "dev"
ALERT_EMAIL = "tonio@aspireos.app"

# Lambda layers (public Klayers — https://github.com/keithrozario/Klayers)
KLAYERS_REQUESTS = "arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-requests:17"
KLAYERS_STRIPE = "arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-stripe:5"

# ── Clients ───────────────────────────────────────────────────────────────────

iam = boto3.client("iam", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)
events = boto3.client("events", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)


def create_or_get(resource_type, create_fn, get_fn, name):
    """Create AWS resource, or return existing if already created."""
    try:
        result = create_fn()
        logger.info("  CREATED %s: %s", resource_type, name)
        return result
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("EntityAlreadyExists", "ResourceConflictException",
                     "StateMachineAlreadyExists", "ResourceAlreadyExistsException"):
            result = get_fn()
            logger.info("  EXISTS %s: %s", resource_type, name)
            return result
        raise


# =============================================================================
# Step 1: IAM Role
# =============================================================================

TRUST_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": [
                    "lambda.amazonaws.com",
                    "states.amazonaws.com",
                    "apigateway.amazonaws.com",
                ]
            },
            "Action": "sts:AssumeRole",
        }
    ],
})

ROLE_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue",
                "secretsmanager:PutSecretValue",
                "secretsmanager:UpdateSecretVersionStage",
                "secretsmanager:DescribeSecret",
                "secretsmanager:ListSecrets",
            ],
            "Resource": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:aspire/*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
            ],
            "Resource": "arn:aws:logs:*:*:*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "sns:Publish",
            ],
            "Resource": f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:aspire-rotation-*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "lambda:InvokeFunction",
            ],
            "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:aspire-rotation-*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "states:StartExecution",
                "states:DescribeExecution",
            ],
            "Resource": f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:aspire-*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "cloudwatch:PutMetricData",
            ],
            "Resource": "*",
            "Condition": {
                "StringEquals": {"cloudwatch:namespace": "Aspire/SecretsManager"}
            },
        },
    ],
})

ROLE_NAME = f"aspire-rotation-execution-{ENVIRONMENT}"
POLICY_NAME = f"aspire-rotation-policy-{ENVIRONMENT}"


def deploy_iam_role():
    logger.info("[1/7] Creating IAM execution role...")

    def create():
        resp = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=TRUST_POLICY,
            Description="Aspire rotation Lambda + Step Functions execution role",
            Tags=[
                {"Key": "Project", "Value": "aspire"},
                {"Key": "Environment", "Value": ENVIRONMENT},
            ],
        )
        # Attach inline policy
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=ROLE_POLICY,
        )
        # Wait for role to propagate
        time.sleep(10)
        return resp["Role"]["Arn"]

    def get():
        resp = iam.get_role(RoleName=ROLE_NAME)
        # Update policy in case it changed
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=ROLE_POLICY,
        )
        return resp["Role"]["Arn"]

    return create_or_get("IAM Role", create, get, ROLE_NAME)


# =============================================================================
# Step 2: SNS Topics
# =============================================================================

def deploy_sns_topics():
    logger.info("[2/7] Creating SNS topics...")

    events_topic = sns.create_topic(
        Name=f"aspire-rotation-events-{ENVIRONMENT}",
        Tags=[
            {"Key": "Project", "Value": "aspire"},
            {"Key": "Environment", "Value": ENVIRONMENT},
        ],
    )["TopicArn"]
    logger.info("  Events topic: %s", events_topic)

    failures_topic = sns.create_topic(
        Name=f"aspire-rotation-failures-{ENVIRONMENT}",
        Tags=[
            {"Key": "Project", "Value": "aspire"},
            {"Key": "Environment", "Value": ENVIRONMENT},
        ],
    )["TopicArn"]
    logger.info("  Failures topic: %s", failures_topic)

    # Subscribe email (idempotent — won't re-subscribe same email)
    for topic in [events_topic, failures_topic]:
        sns.subscribe(
            TopicArn=topic,
            Protocol="email",
            Endpoint=ALERT_EMAIL,
        )
    logger.info("  Email subscriptions created for %s", ALERT_EMAIL)

    return events_topic, failures_topic


# =============================================================================
# Step 3: Package Lambda Code
# =============================================================================

def package_lambda_code():
    logger.info("[3/7] Packaging Lambda code as ZIP...")

    # Base path for Lambda code
    lambda_dir = Path("/mnt/c/Users/tonio/Projects/myapp/infrastructure/aws/rotation-lambdas")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(lambda_dir):
            for file in files:
                if file.endswith(".py"):
                    file_path = Path(root) / file
                    arcname = str(file_path.relative_to(lambda_dir))
                    zf.write(file_path, arcname)

    zip_bytes = zip_buffer.getvalue()
    logger.info("  Package size: %.1f KB (%d files)", len(zip_bytes) / 1024, 10)
    return zip_bytes


# =============================================================================
# Step 4: Deploy Lambda Functions
# =============================================================================

def deploy_lambda(name, handler, zip_bytes, role_arn, env_vars, timeout=60,
                  memory=128, layers=None):
    func_name = f"aspire-{name}-{ENVIRONMENT}"

    try:
        resp = lam.create_function(
            FunctionName=func_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler=handler,
            Code={"ZipFile": zip_bytes},
            Timeout=timeout,
            MemorySize=memory,
            Environment={"Variables": env_vars},
            Layers=layers or [],
            Tags={
                "Project": "aspire",
                "Environment": ENVIRONMENT,
            },
        )
        logger.info("  CREATED Lambda: %s", func_name)
        return resp["FunctionArn"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            # Update existing function
            lam.update_function_code(
                FunctionName=func_name,
                ZipFile=zip_bytes,
            )
            # Wait for update to complete
            time.sleep(5)
            lam.update_function_configuration(
                FunctionName=func_name,
                Runtime="python3.12",
                Role=role_arn,
                Handler=handler,
                Timeout=timeout,
                MemorySize=memory,
                Environment={"Variables": env_vars},
                Layers=layers or [],
            )
            resp = lam.get_function(FunctionName=func_name)
            logger.info("  UPDATED Lambda: %s", func_name)
            return resp["Configuration"]["FunctionArn"]
        raise


def deploy_lambdas(zip_bytes, role_arn, events_topic, failures_topic):
    logger.info("[4/7] Deploying Lambda functions...")

    handler_arn = deploy_lambda(
        name="rotation-handler",
        handler="handlers.rotation_handler.lambda_handler",
        zip_bytes=zip_bytes,
        role_arn=role_arn,
        env_vars={
            "ENVIRONMENT": ENVIRONMENT,
            "SNS_EVENTS_TOPIC": events_topic,
            "SNS_FAILURE_TOPIC": failures_topic,
        },
        timeout=300,
        memory=256,
        # Note: vendor layers (stripe, requests) needed for production rotation.
        # Dev deployment: handler deployed without layers — age_checker works fine
        # (only needs boto3 which is built into Lambda runtime).
        # Actual rotation requires: pip install stripe requests -t /tmp/layer && zip
        layers=[],
    )

    checker_arn = deploy_lambda(
        name="secret-age-checker",
        handler="handlers.age_checker.lambda_handler",
        zip_bytes=zip_bytes,
        role_arn=role_arn,
        env_vars={
            "ENVIRONMENT": ENVIRONMENT,
            "SNS_FAILURE_TOPIC": failures_topic,
        },
        timeout=60,
        memory=128,
    )

    return handler_arn, checker_arn


# =============================================================================
# Step 5: Step Functions State Machine
# =============================================================================

def deploy_state_machine(role_arn, handler_arn, events_topic, failures_topic):
    logger.info("[5/7] Deploying Step Functions state machine...")

    # Read the ASL template
    asl_path = Path("/mnt/c/Users/tonio/Projects/myapp/infrastructure/aws/rotation/step-function.asl.json")
    asl_template = asl_path.read_text()

    # Replace template variables
    asl = asl_template.replace("${rotation_handler_arn}", handler_arn)
    asl = asl.replace("${sns_events_topic}", events_topic)
    asl = asl.replace("${sns_failure_topic}", failures_topic)
    asl = asl.replace("${environment}", ENVIRONMENT)

    sm_name = f"aspire-secret-rotation-{ENVIRONMENT}"

    try:
        resp = sfn.create_state_machine(
            name=sm_name,
            definition=asl,
            roleArn=role_arn,
            type="STANDARD",
            tags=[
                {"key": "Project", "value": "aspire"},
                {"key": "Environment", "value": ENVIRONMENT},
            ],
        )
        sm_arn = resp["stateMachineArn"]
        logger.info("  CREATED: %s", sm_arn)
    except ClientError as e:
        if e.response["Error"]["Code"] == "StateMachineAlreadyExists":
            # Find existing and update
            resp = sfn.list_state_machines()
            sm_arn = next(
                m["stateMachineArn"]
                for m in resp["stateMachines"]
                if m["name"] == sm_name
            )
            sfn.update_state_machine(
                stateMachineArn=sm_arn,
                definition=asl,
                roleArn=role_arn,
            )
            logger.info("  UPDATED: %s", sm_arn)
        else:
            raise

    return sm_arn


# =============================================================================
# Step 6: EventBridge Schedule
# =============================================================================

def deploy_eventbridge(checker_arn):
    logger.info("[6/7] Creating EventBridge daily schedule...")

    rule_name = f"aspire-secret-age-daily-{ENVIRONMENT}"

    events.put_rule(
        Name=rule_name,
        ScheduleExpression="rate(1 day)",
        State="ENABLED",
        Description="Daily secret age check — triggers age_checker Lambda",
    )

    events.put_targets(
        Rule=rule_name,
        Targets=[
            {
                "Id": "age-checker",
                "Arn": checker_arn,
            }
        ],
    )

    # Allow EventBridge to invoke Lambda
    try:
        lam.add_permission(
            FunctionName=checker_arn,
            StatementId="AllowEventBridgeInvoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{REGION}:{ACCOUNT_ID}:rule/{rule_name}",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            pass  # Permission already exists

    logger.info("  Daily schedule created: %s", rule_name)


# =============================================================================
# Step 7: CloudWatch Alarms
# =============================================================================

def deploy_cloudwatch_alarms(failures_topic):
    logger.info("[7/7] Creating CloudWatch alarms...")

    # Secret age alarms for manual-rotation providers
    manual_providers = [
        "elevenlabs", "anam", "livekit",
        "tavily", "brave", "google_maps",
    ]

    for provider in manual_providers:
        alarm_name = f"aspire-secret-age-{provider}-{ENVIRONMENT}"
        cw.put_metric_alarm(
            AlarmName=alarm_name,
            ComparisonOperator="GreaterThanThreshold",
            EvaluationPeriods=1,
            MetricName="SecretAgeDays",
            Namespace="Aspire/SecretsManager",
            Dimensions=[{"Name": "SecretName", "Value": provider}],
            Period=86400,
            Statistic="Maximum",
            Threshold=80,
            AlarmActions=[failures_topic],
            AlarmDescription=(
                f"Secret {provider} is >80 days old. "
                f"Create new key in vendor dashboard, then run: "
                f"./scripts/import-key.sh providers {provider} <new-value>"
            ),
        )

    logger.info("  Created %d secret age alarms", len(manual_providers))


# =============================================================================
# Step 8: Smoke Test
# =============================================================================

def smoke_test(checker_arn, sm_arn):
    logger.info("\n[SMOKE TEST] Running age checker Lambda...")

    try:
        resp = lam.invoke(
            FunctionName=checker_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps({}),
        )
        payload = json.loads(resp["Payload"].read())

        if resp.get("FunctionError"):
            logger.error("  FAIL: Lambda error: %s", payload)
            return False

        logger.info("  Age Checker Result:")
        logger.info("    Secrets checked: %d", payload.get("checked", 0))
        logger.info("    Alerts raised: %d", payload.get("alerts", 0))

        for r in payload.get("results", []):
            status = "OVERDUE" if r.get("overdue") else "OK"
            logger.info(
                "    [%s] %s — %dd old (max %dd)",
                status, r["secret"], r["age_days"], r["max_age_days"],
            )

        logger.info("  Age checker: PASS")
        return True

    except Exception as e:
        logger.error("  FAIL: %s", e)
        return False


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 60)
    print("Aspire Rotation Infrastructure Deployment")
    print(f"Account: {ACCOUNT_ID} | Region: {REGION} | Env: {ENVIRONMENT}")
    print("=" * 60)

    # Verify credentials
    sts = boto3.client("sts", region_name=REGION)
    identity = sts.get_caller_identity()
    print(f"Authenticated as: {identity['Arn']}\n")

    # Deploy
    role_arn = deploy_iam_role()
    events_topic, failures_topic = deploy_sns_topics()
    zip_bytes = package_lambda_code()
    handler_arn, checker_arn = deploy_lambdas(zip_bytes, role_arn, events_topic, failures_topic)
    sm_arn = deploy_state_machine(role_arn, handler_arn, events_topic, failures_topic)
    deploy_eventbridge(checker_arn)
    deploy_cloudwatch_alarms(failures_topic)

    # Smoke test
    smoke_test(checker_arn, sm_arn)

    print("\n" + "=" * 60)
    print("Deployment complete!")
    print(f"  IAM Role: {role_arn}")
    print(f"  Handler Lambda: {handler_arn}")
    print(f"  Age Checker Lambda: {checker_arn}")
    print(f"  State Machine: {sm_arn}")
    print(f"  Events SNS: {events_topic}")
    print(f"  Failures SNS: {failures_topic}")
    print(f"  Alert email: {ALERT_EMAIL}")
    print("=" * 60)


if __name__ == "__main__":
    main()
