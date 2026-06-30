#!/usr/bin/env python3
"""
Simple Claude API Code Generator - No cut-offs!
"""

import os
from anthropic import Anthropic

# Initialize client
client = Anthropic()

def generate_complete_code(prompt):
    """Generate code without interruptions"""
    
    print("\n🚀 Generating your code...\n")
    print("=" * 60)
    
    # Use max_tokens to get the full response
    response = client.messages.create(
        model="claude-sonnet-4-6",  # Best for coding
        max_tokens=64000,  # Maximum for complete code (no cut-offs!)
        messages=[
            {"role": "user", "content": f"Provide the COMPLETE code. No placeholders. No 'rest of code remains same'. Full file only.\n\n{prompt}"}
        ]
    )
    
    # Extract the code
    code = response.content[0].text
    
    print(code)
    print("\n" + "=" * 60)
    print(f"✅ Complete! Generated {len(code)} characters")
    print(f"💰 Cost: ~${response.usage.output_tokens * 0.000015:.4f} (output tokens: {response.usage.output_tokens})")
    
    return code

if __name__ == "__main__":
    # Replace this with YOUR prompt
    prompt = """Write a complete React component for a responsive navigation bar.
    Include:
    - Mobile hamburger menu
    - Desktop horizontal menu
    - Active link highlighting
    - Tailwind CSS styling
    - Full TypeScript
    Give the ENTIRE file, no placeholders."""
    
    code = generate_complete_code(prompt)
    
    # Save to file
    with open("generated_code.tsx", "w") as f:
        f.write(code)
    print(f"\n💾 Saved to generated_code.tsx")
