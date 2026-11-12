import json

raw_from_openclaw = '"{\\"success\\":true,\\"verifiedText\\":\\"hello\\"}"'
print("Raw type:", type(raw_from_openclaw))

eval_data = raw_from_openclaw
while isinstance(eval_data, str):
    try:
        eval_data = json.loads(eval_data)
    except Exception:
        break

print("Unpacked type:", type(eval_data))
print("Is dict:", isinstance(eval_data, dict))
print("Success:", eval_data.get("success") if isinstance(eval_data, dict) else None)

