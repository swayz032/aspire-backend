"""Build clean Ava ElevenLabs prompt from plan sections. No plan text leaks."""
import os

plan_path = 'C:/Users/tonio/.claude/plans/polymorphic-roaming-parnas.md'
with open(plan_path, 'r', encoding='utf-8') as f:
    plan = f.read()

# Extract invoke_quinn section
m1 = '### invoke_quinn tool \u2014 replace with:\n```\n'
s1 = plan.find(m1) + len(m1)
e1 = plan.find('\n```', s1)
invoke_quinn = plan[s1:e1].strip()

# Extract examples section
m2 = '### Backend tasks examples \u2014 replace with:\n```\n'
s2 = plan.find(m2) + len(m2)
e2 = plan.find('\n```', s2)
examples = plan[s2:e2].strip()

# Read base prompt (the one we know was good before the leak)
# We'll rebuild from the known-good structure
base_path = 'C:/Users/tonio/Projects/myapp/backend/orchestrator/src/aspire_orchestrator/config/pack_personas/ava_elevenlabs_prompt.md'

# Read current (broken) prompt to get the non-invoke sections
with open(base_path, 'r', encoding='utf-8') as f:
    current = f.read()

# Find where the plan text starts leaking
leak_marker = 'lan: Quinn'
if leak_marker in current:
    # Cut everything from the leak point
    clean_end = current.find(leak_marker)
    # Back up to find the last valid section before the leak
    # The leak happens after ava_execute_action
    exec_marker = '## ava_execute_action'
    exec_idx = current.find(exec_marker)
    if exec_idx > 0:
        # Find end of ava_execute_action section
        next_section = current.find('\n## ', exec_idx + len(exec_marker))
        if next_section > 0:
            before_leak = current[:next_section].rstrip()
        else:
            before_leak = current[:exec_idx + 200].rstrip()
    else:
        before_leak = current[:clean_end].rstrip()

    # Find where invoke_adam starts (after the leaked text)
    adam_marker = '## invoke_adam'
    adam_idx = current.find(adam_marker)

    # Get everything after invoke_adam
    if adam_idx > 0:
        after_tools = current[adam_idx:]
    else:
        after_tools = ""

    # Rebuild: before_leak + invoke_quinn + after_tools
    clean_prompt = before_leak + '\n\n' + invoke_quinn + '\n\n' + after_tools
else:
    # No leak found - just replace invoke_quinn section
    old_iq = '## invoke_quinn\n\nUse when the user needs invoices created, quotes generated, payment status checked, or client billing managed. Tell the user "I\'ll get Quinn on that" before calling.'
    if old_iq in current:
        clean_prompt = current.replace(old_iq, invoke_quinn)
    else:
        clean_prompt = current

# Also replace the old examples
old_example = 'Example:\nUser: "Create an invoice for Acme Corp."\nYou: "I\'ll get Quinn on that." Call invoke_quinn. Then relay what Quinn produced.'
if old_example in clean_prompt:
    clean_prompt = clean_prompt.replace(old_example, examples)

# Make sure math/amount rules are in Tone
if 'Do basic math yourself' not in clean_prompt:
    # Add before Banned phrases
    clean_prompt = clean_prompt.replace(
        '## Banned phrases',
        '- When stating dollar amounts, spell them out fully for voice: "nine hundred fifty dollars" not "$950". Never use dollar signs or commas in amounts \u2014 the voice system may misread them.\n- Do basic math yourself before calling agents. If user says "a hundred pallets at nine fifty each" \u2014 you calculate: "That\'s nine hundred fifty for the pallets."\n\n## Banned phrases'
    )

# Verify no plan text
assert 'Sentry' not in clean_prompt, "Plan text leaked: Sentry"
assert 'BACKEND' not in clean_prompt, "Plan text leaked: BACKEND"
assert 'Root cause' not in clean_prompt, "Plan text leaked: Root cause"
assert 'lan: Quinn' not in clean_prompt, "Plan title leaked"
assert 'invoke_quinn' in clean_prompt, "Missing invoke_quinn"
assert 'invoke_adam' in clean_prompt, "Missing invoke_adam"
assert 'invoke_tec' in clean_prompt, "Missing invoke_tec"
assert 'pallets' in clean_prompt, "Missing quantity examples"

with open(base_path, 'w', encoding='utf-8') as f:
    f.write(clean_prompt.strip() + '\n')

print(f'Clean prompt: {len(clean_prompt)} chars')
print('All assertions passed - no plan text leaked')
