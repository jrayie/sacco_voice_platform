"""
voice/claude_client.py

Sends caller transcripts to Claude (via AWS Bedrock) for:
  - Language detection (English / Kiswahili / mixed)
  - Intent classification
  - Amount extraction
  - Generating a natural-language response in the caller's language
"""

import json
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

# ── Bedrock configuration ────────────────────────────────────────────────────
BEDROCK_REGION = "us-east-1"
MODEL_ID = "us.anthropic.claude-sonnet-4-20260514-v1:0"

# ── Prompt template ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a voice assistant for a Kenyan SACCO (savings and credit cooperative). \
You understand English and Kiswahili naturally, including common code-switching \
(e.g. "Nataka loan" or "My salio ni ngapi").

Respond with ONLY valid JSON — no markdown, no explanation, no preamble. \
Use exactly this format:

{
    "detected_language": "sw" or "en",
    "intent": one of [
        "check_balance",
        "check_loan_balance",
        "apply_loan",
        "loan_eligibility",
        "repayment_schedule",
        "interest_rate",
        "speak_to_agent",
        "exit"
    ],
    "amount": null or a number (extract from text, e.g. "elfu kumi" → 10000),
    "response_text": "A warm, natural response to the caller in the detected language"
}

Examples:
- "Salio yangu ni ngapi?"  → intent: check_balance, detected_language: sw,
  response_text: "Salio yako ni KSh 12,500."
- "I want a loan of 10,000" → intent: apply_loan, amount: 10000, detected_language: en,
  response_text: "We are processing your loan application for KSh 10,000. You will receive an SMS shortly."
- "Nataka mkopo wa elfu tano" → intent: apply_loan, amount: 5000, detected_language: sw,
  response_text: "Ombi lako la mkopo wa KSh 5,000 linachakatwa. Utapokea ujumbe kwa SMS hivi karibuni."
- "What is the interest rate?" → intent: interest_rate, detected_language: en,
  response_text: "Our current loan interest rate is 12% per annum, reducing balance."

Always respond in the same language the caller used."""


def understand_with_claude(transcribed_text: str) -> dict:
    """
    Send a caller's transcript to Claude on AWS Bedrock and return structured data.

    Args:
        transcribed_text: Raw text from speech-to-text (English, Kiswahili, or mixed).

    Returns:
        A dict with keys:
            - intent          (str)  : classified caller intent
            - amount          (int|None): monetary amount mentioned, or None
            - language        (str)  : 'en' or 'sw'
            - response_text   (str)  : natural-language reply for the caller

    On any error, returns a safe fallback dict so the call can still be handled.
    """

    if not transcribed_text or not transcribed_text.strip():
        logger.warning("understand_with_claude called with empty transcript")
        return _fallback_response("en")

    # Build the user message with the actual transcript
    user_message = f'The caller said: "{transcribed_text.strip()}"'

    # Construct the Bedrock request body (Claude Messages API format)
    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 512,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_message}
        ],
    }

    try:
        client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

        response = client.invoke_model(
            modelId=MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request_body),
        )

        raw_body = response["body"].read().decode("utf-8")
        bedrock_response = json.loads(raw_body)

        # Extract the text content from Claude's response
        content_blocks = bedrock_response.get("content", [])
        claude_text = ""
        for block in content_blocks:
            if block.get("type") == "text":
                claude_text += block.get("text", "")

        claude_text = claude_text.strip()
        logger.debug("Claude raw response: %s", claude_text)

        # Parse the JSON Claude returned
        parsed = json.loads(claude_text)

        # Normalise to the shape our views expect
        return {
            "intent": parsed.get("intent", "speak_to_agent"),
            "amount": parsed.get("amount"),
            "language": parsed.get("detected_language", "en"),
            "response_text": parsed.get(
                "response_text",
                _default_response_text(parsed.get("detected_language", "en")),
            ),
        }

    except (BotoCoreError, ClientError) as exc:
        logger.error("AWS Bedrock error: %s", exc, exc_info=True)
        # Try to detect language from text for a contextual fallback
        lang = _guess_language(transcribed_text)
        return _fallback_response(lang)

    except json.JSONDecodeError as exc:
        logger.error(
            "Failed to parse Claude JSON response: %s | raw: %s",
            exc,
            claude_text,
            exc_info=True,
        )
        lang = _guess_language(transcribed_text)
        return _fallback_response(lang)

    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Unexpected error in understand_with_claude: %s", exc, exc_info=True)
        return _fallback_response("en")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _guess_language(text: str) -> str:
    """
    Very lightweight heuristic to guess the language when Claude is unavailable.
    Looks for common Kiswahili words in the transcript.
    """
    sw_keywords = {
        "nataka", "salio", "mkopo", "tafadhali", "asante", "habari",
        "ombi", "huduma", "yangu", "yako", "elfu", "shilingi", "karibu",
    }
    words = set(text.lower().split())
    if words & sw_keywords:
        return "sw"
    return "en"


def _default_response_text(language: str) -> str:
    """Generic acknowledgement when we have an intent but no response_text."""
    if language == "sw":
        return "Ombi lako limepokelewa. Tafadhali subiri."
    return "Your request has been received. Please hold on."


def _fallback_response(language: str) -> dict:
    """
    Safe fallback returned when Claude or Bedrock is unavailable.
    Routes the caller to a human agent.
    """
    if language == "sw":
        msg = (
            "Samahani, kuna tatizo la kiufundi. "
            "Tafadhali subiri, tutakuunganisha na wakala wetu."
        )
    else:
        msg = (
            "We're sorry, there is a technical issue. "
            "Please hold while we connect you to an agent."
        )

    return {
        "intent": "speak_to_agent",
        "amount": None,
        "language": language,
        "response_text": msg,
    }
