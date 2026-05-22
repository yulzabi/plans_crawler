"""Bedrock extractor: sends permit page images to Claude Sonnet 4 for structured extraction."""
import json
import base64
import time
import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = '''This is an Israeli building permit form (טופס 3 - היתר בנייה). Extract these fields and return ONLY valid JSON.

Fields:
- owner: בעל ההיתר - the person/company granted the permit
- architect: עורך הבקשה - the architect who prepared the application
- address: כתובת הבניה - the street name and house number where construction takes place
- gush: גוש - land block number (3-5 digits, found near חלקה/מגרש)
- helka: חלקה - parcel number (found near גוש/מגרש)
- migrash: מגרש - lot number
- permit_number: מספר בקשה - the application number (typically 7 digits)
- date: תאריך - permit approval date
- city_plan: תוכנית/תב"ע - the city building plan number

Important:
- address is the CONSTRUCTION SITE (כתובת הבניה), not the owner's address
- gush/helka/migrash are land registry numbers (3-5 digits). Don't confuse with the file ID (תיק בנין).
- Use null if not clearly visible. Do NOT guess.'''

FIELDS = ["owner", "architect", "address", "gush", "helka", "migrash",
          "permit_number", "date", "city_plan"]


class BedrockExtractor:
    def __init__(self, region: str = "us-east-1",
                 model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"):
        self.model_id = model_id
        self.client = boto3.client("bedrock-runtime", region_name=region)

    def extract(self, image_bytes: bytes) -> tuple[dict, dict]:
        """Extract fields from a permit page image.
        
        Returns (fields_dict, confidence_dict).
        fields_dict: field_name -> value (str) or ""
        confidence_dict: field_name -> "high" | "none"
        """
        image_b64 = base64.b64encode(image_bytes).decode()

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": EXTRACTION_PROMPT}
            ]}]
        })

        response_text = self._invoke_with_retry(body)
        return self._parse_response(response_text)

    def _invoke_with_retry(self, body: str, max_retries: int = 3) -> str:
        """Invoke Bedrock with exponential backoff on throttling."""
        for attempt in range(max_retries):
            try:
                resp = self.client.invoke_model(modelId=self.model_id, body=body)
                result = json.loads(resp["body"].read())
                return result["content"][0]["text"]
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "ThrottlingException" and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"Throttled, retrying in {wait}s...")
                    time.sleep(wait)
                elif code == "ExpiredTokenException":
                    raise RuntimeError(
                        "AWS credentials expired. Run: ada credentials update --account yulazari@amazon.com --role admin --once"
                    ) from e
                else:
                    raise

    def _parse_response(self, text: str) -> tuple[dict, dict]:
        """Parse JSON response into fields and confidence dicts."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from Bedrock, returning empty")
            return {f: "" for f in FIELDS}, {f: "none" for f in FIELDS}

        fields = {}
        confidence = {}
        for f in FIELDS:
            val = parsed.get(f)
            if val is None or val == "null":
                fields[f] = ""
                confidence[f] = "none"
            else:
                fields[f] = str(val).strip()
                confidence[f] = "high"

        return fields, confidence
