"""Batch processor: submit multiple PDFs to Bedrock batch inference for 50% cost savings."""
import json
import base64
import time
import uuid
import logging
from pathlib import Path

import boto3

from src.pdf_processor import find_permit_pages, render_page, PermitRecord
from src.bedrock_extractor import EXTRACTION_PROMPT, FIELDS

logger = logging.getLogger(__name__)

BATCH_BUCKET = "plans-crawler-batch"
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


class BatchProcessor:
    def __init__(self, region: str = "us-east-1", bucket: str = BATCH_BUCKET, model_id: str = DEFAULT_MODEL_ID):
        self.region = region
        self.bucket = bucket
        self.model_id = model_id
        self.bedrock = boto3.client("bedrock", region_name=region)
        self.s3 = boto3.client("s3", region_name=region)

    def prepare_batch(self, pdf_paths: list[str], output_dir: str = "/tmp/batch") -> str:
        """Render PDFs and create JSONL input file. Returns local JSONL path."""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        jsonl_path = f"{output_dir}/batch_input.jsonl"

        with open(jsonl_path, "w") as f:
            for pdf_path in pdf_paths:
                pages = find_permit_pages(pdf_path)
                if not pages:
                    continue
                img = render_page(pdf_path, pages[0])
                image_b64 = base64.b64encode(img).decode()

                record = {
                    "recordId": pdf_path,
                    "modelInput": {
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": [
                            {"type": "image", "source": {
                                "type": "base64", "media_type": "image/png", "data": image_b64}},
                            {"type": "text", "text": EXTRACTION_PROMPT}
                        ]}]
                    }
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.info(f"Prepared batch JSONL: {jsonl_path}")
        return jsonl_path

    def submit_batch(self, jsonl_path: str, role_arn: str, s3_key: str = None) -> str:
        """Upload JSONL to S3 (or reuse existing) and submit batch job. Returns job ARN."""
        job_id = f"permits-{uuid.uuid4().hex[:8]}"

        if s3_key:
            s3_input = f"s3://{self.bucket}/{s3_key}"
        else:
            s3_key = f"input/{job_id}.jsonl"
            s3_input = f"s3://{self.bucket}/{s3_key}"
            self.s3.upload_file(jsonl_path, self.bucket, s3_key)
            logger.info(f"Uploaded to {s3_input}")

        s3_output = f"s3://{self.bucket}/output/{job_id}/"

        # Submit job
        response = self.bedrock.create_model_invocation_job(
            roleArn=role_arn,
            modelId=self.model_id,
            jobName=job_id,
            inputDataConfig={"s3InputDataConfig": {"s3Uri": s3_input}},
            outputDataConfig={"s3OutputDataConfig": {"s3Uri": s3_output}},
        )
        job_arn = response["jobArn"]
        logger.info(f"Submitted batch job: {job_arn}")
        return job_arn

    def wait_for_job(self, job_arn: str, poll_interval: int = 60) -> str:
        """Poll until job completes. Returns status."""
        while True:
            resp = self.bedrock.get_model_invocation_job(jobIdentifier=job_arn)
            status = resp["status"]
            if status in ("Completed", "Failed", "Stopped"):
                return status
            logger.info(f"Job status: {status}, waiting {poll_interval}s...")
            time.sleep(poll_interval)

    def parse_results(self, job_arn: str) -> list[tuple[str, PermitRecord, dict]]:
        """Download and parse batch output. Returns list of (pdf_path, record, confidence)."""
        resp = self.bedrock.get_model_invocation_job(jobIdentifier=job_arn)
        output_uri = resp["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"]

        # List output files
        prefix = output_uri.replace(f"s3://{self.bucket}/", "")
        objs = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)

        results = []
        for obj in objs.get("Contents", []):
            if not obj["Key"].endswith(".jsonl.out"):
                continue
            body = self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read()
            for line in body.decode().strip().split("\n"):
                parsed = json.loads(line)
                pdf_path = parsed["recordId"]
                output = parsed.get("modelOutput", {})
                text = output.get("content", [{}])[0].get("text", "{}")

                # Parse the model's JSON response
                try:
                    cleaned = text.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
                    fields_data = json.loads(cleaned)
                except json.JSONDecodeError:
                    fields_data = {}

                fields = {}
                confidence = {}
                for field in FIELDS:
                    val = fields_data.get(field)
                    if val is None or val == "null":
                        fields[field] = ""
                        confidence[field] = "none"
                    else:
                        fields[field] = str(val).strip()
                        confidence[field] = "high"

                record = PermitRecord(
                    permit_number=fields.get("permit_number", ""),
                    address=fields.get("address", ""),
                    gush=fields.get("gush", ""),
                    helka=fields.get("helka", ""),
                    migrash=fields.get("migrash", ""),
                    city_plan=fields.get("city_plan", ""),
                    owner=fields.get("owner", ""),
                    architect=fields.get("architect", ""),
                    pdf_path=pdf_path,
                    date=fields.get("date", ""),
                )
                results.append((pdf_path, record, confidence))

        return results


def run_batch(pdf_paths: list[str], role_arn: str, region: str = "us-east-1", model_id: str = DEFAULT_MODEL_ID):
    """Convenience function: prepare, submit, wait, parse batch job."""
    processor = BatchProcessor(region=region, model_id=model_id)

    # Check for existing upload
    existing_key = None
    try:
        resp = processor.s3.list_objects_v2(Bucket=processor.bucket, Prefix="input/", MaxKeys=5)
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith(".jsonl"):
                size_mb = obj["Size"] / 1024 / 1024
                print(f"📂 Found existing upload: {obj['Key']} ({size_mb:.0f}MB)")
                reuse = input("   Reuse it? [Y/n] ").strip().lower()
                if reuse != "n":
                    existing_key = obj["Key"]
                break
    except Exception:
        pass

    if existing_key:
        print(f"🚀 Submitting batch job with existing data...")
        job_arn = processor.submit_batch(None, role_arn, s3_key=existing_key)
    else:
        print(f"📦 Preparing batch for {len(pdf_paths)} PDFs...")
        jsonl_path = processor.prepare_batch(pdf_paths)
        print(f"🚀 Submitting batch job...")
        job_arn = processor.submit_batch(jsonl_path, role_arn)

    print(f"⏳ Waiting for completion (job: {job_arn})...")
    status = processor.wait_for_job(job_arn)

    if status != "Completed":
        print(f"❌ Batch job {status}")
        return []

    print(f"✅ Parsing results...")
    results = processor.parse_results(job_arn)
    print(f"📊 Got {len(results)} results")
    return results
