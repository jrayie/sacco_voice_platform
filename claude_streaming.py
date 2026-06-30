#!/usr/bin/env python3
"""
Claude Streaming - Watch code generate in real-time!
"""

from anthropic import Anthropic

client = Anthropic()

print("\n🎨 Claude is writing your code...\n")
print("─" * 50)

with client.messages.stream(
    model="claude-sonnet-4-6",
    max_tokens=64000,
    messages=[
        {"role": "user", "content": "Write a complete Python script that fetches data from a REST API and saves to CSV. Include error handling and comments. FULL CODE only, no placeholders."}
    ],
) as stream:
    full_response = []
    for text in stream.text_stream:
        print(text, end="", flush=True)
        full_response.append(text)

print("\n" + "─" * 50)
print("\n✅ Complete!")

# Save the response
with open("api_fetcher.py", "w") as f:
    f.write("".join(full_response))
print("💾 Saved to api_fetcher.py")
