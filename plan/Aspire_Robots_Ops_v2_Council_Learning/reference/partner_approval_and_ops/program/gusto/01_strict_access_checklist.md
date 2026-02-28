# Strict Access Checklist

Requirements:
- Tokens must be bound to a single company
- Cross-company access attempts must fail
- Provide verification evidence (endpoint test results)

Implementation:
- store company_id on token record immutably
- enforce company_id match on every request
- automated test for cross-company denial
