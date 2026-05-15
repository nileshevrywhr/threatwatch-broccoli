import os
import logging
import time
import json
import smtplib
import re
import socket
import ipaddress
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from celery import Task
from supabase import create_client, Client
from googleapiclient.discovery import build
from fpdf import FPDF
from celery_app import app
from utils.schedule_utils import calculate_next_run_at
from google import genai
from openai import OpenAI
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Configure logging
logger = logging.getLogger(__name__)

# Supabase Client Setup
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {e}")

# Google CSE Setup
GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY")
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

MAX_SOURCE_ITEMS = int(os.environ.get("THREATWATCH_MAX_SOURCE_ITEMS", 5))
MAX_FETCH_BYTES = int(os.environ.get("THREATWATCH_MAX_FETCH_BYTES", 200000))
FETCH_TIMEOUT_SECONDS = int(os.environ.get(
    "THREATWATCH_FETCH_TIMEOUT_SECONDS", 8))
USER_AGENT = "ThreatWatchBot/0.1"


class _SourceHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts = []
        self.text_parts = []
        self.meta = {}
        self._capture_title = False
        self._skip_text = False

    def handle_starttag(self, tag, attrs):
        attrs_map = {key.lower(): value for key, value in attrs}

        if tag.lower() == "title":
            self._capture_title = True
            return

        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_text = True
            return

        if tag.lower() == "meta":
            name = (attrs_map.get("name") or attrs_map.get(
                "property") or "").strip().lower()
            content = (attrs_map.get("content") or "").strip()
            if name and content and name not in self.meta:
                self.meta[name] = content

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._capture_title = False
        elif tag.lower() in {"script", "style", "noscript"}:
            self._skip_text = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return

        if self._capture_title:
            self.title_parts.append(text)
        elif not self._skip_text:
            self.text_parts.append(text)


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def _is_public_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False

        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False

        if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
            return False

        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return False
        except ValueError:
            pass

        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            for _, _, _, _, sockaddr in socket.getaddrinfo(host, port):
                resolved_ip = ipaddress.ip_address(sockaddr[0])
                if resolved_ip.is_private or resolved_ip.is_loopback or resolved_ip.is_link_local or resolved_ip.is_multicast or resolved_ip.is_reserved:
                    return False
        except socket.gaierror:
            return False

        return True
    except Exception:
        return False


def _fetch_source_document(url: str) -> dict | None:
    normalized_url = _normalize_url(url)
    if not _is_public_http_url(normalized_url):
        logger.warning(f"Skipping unsafe source URL: {normalized_url}")
        return None

    request = Request(
        normalized_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if not any(token in content_type for token in ("text/html", "text/plain", "application/xhtml+xml")):
                logger.warning(
                    f"Skipping non-text source URL: {normalized_url} ({content_type})")
                return None

            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit() and int(content_length) > MAX_FETCH_BYTES:
                logger.warning(
                    f"Skipping oversized source URL: {normalized_url}")
                return None

            raw_bytes = response.read(MAX_FETCH_BYTES + 1)
            if len(raw_bytes) > MAX_FETCH_BYTES:
                logger.warning(
                    f"Truncated oversized source URL: {normalized_url}")
                return None

            charset = response.headers.get_content_charset() or "utf-8"
            text = raw_bytes.decode(charset, errors="replace")
            return {
                "url": normalized_url,
                "content_type": content_type,
                "text": text,
                "headers": dict(response.headers),
            }
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        logger.warning(f"Failed to fetch source URL {normalized_url}: {exc}")
        return None
    except Exception as exc:
        logger.warning(f"Unexpected fetch failure for {normalized_url}: {exc}")
        return None


def _extract_source_evidence(item: dict, source_document: dict | None) -> dict:
    source_url = _normalize_url(item.get("link", ""))
    parsed_url = urlparse(source_url)
    title = item.get("title") or "No Title"
    snippet = item.get("snippet") or ""
    published_at = None
    extracted_text = ""
    source_description = ""

    if source_document:
        parser = _SourceHTMLParser()
        parser.feed(source_document.get("text", ""))
        extracted_title = " ".join(parser.title_parts).strip()
        if extracted_title:
            title = extracted_title

        extracted_text = " ".join(parser.text_parts)
        extracted_text = re.sub(r"\s+", " ", extracted_text).strip()

        published_at = (
            parser.meta.get("article:published_time")
            or parser.meta.get("og:published_time")
            or parser.meta.get("pubdate")
            or parser.meta.get("publish-date")
            or parser.meta.get("date")
        )
        source_description = parser.meta.get(
            "description") or parser.meta.get("og:description") or ""

    excerpt_source = extracted_text or snippet
    excerpt = excerpt_source[:1000]
    text_for_scoring = " ".join(
        part for part in [title, snippet, extracted_text, source_description] if part)

    return {
        "source_id": f"src-{abs(hash(source_url))}",
        "title": title,
        "url": source_url,
        "domain": parsed_url.netloc,
        "published_at": published_at,
        "snippet": snippet,
        "excerpt": excerpt,
        "extracted_text": extracted_text,
        "content_type": source_document.get("content_type") if source_document else None,
        "source_quality": "fetched" if source_document else "snippet_only",
        "score": _calculate_score({"title": title, "snippet": text_for_scoring}, datetime.now(timezone.utc)),
    }


def _build_source_context(evidence_items: list[dict]) -> str:
    context = []
    for idx, item in enumerate(evidence_items, 1):
        context.append(
            f"{idx}. TITLE: {item.get('title', 'No Title')}\n"
            f"   URL: {item.get('url', '#')}\n"
            f"   DOMAIN: {item.get('domain', 'unknown')}\n"
            f"   PUBLISHED_AT: {item.get('published_at') or 'unknown'}\n"
            f"   SCORE: {item.get('score', 0)}\n"
            f"   EXCERPT: {item.get('excerpt', '')}\n"
        )
    return "\n".join(context)


def _extract_json_payload(text: str) -> dict | None:
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start: end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _normalize_structured_report(report_data: dict, query_text: str, ranked_items: list[dict]) -> dict:
    threats = report_data.get("ranked_threats") or []
    if not isinstance(threats, list):
        threats = []

    normalized_threats = []
    for index, threat in enumerate(threats, 1):
        if not isinstance(threat, dict):
            continue
        normalized_threats.append({
            "rank": threat.get("rank") or index,
            "title": threat.get("title") or threat.get("threat_title") or "Unknown threat",
            "impact_score": threat.get("impact_score", 0),
            "confidence_score": threat.get("confidence_score", 0),
            "urgency": threat.get("urgency", "medium"),
            "affected_assets": threat.get("affected_assets", []),
            "attack_vector": threat.get("attack_vector", "unknown"),
            "evidence_source_ids": threat.get("evidence_source_ids", []),
            "rationale": threat.get("rationale", ""),
            "mitigation_now": threat.get("mitigation_now", []),
            "mitigation_24h": threat.get("mitigation_24h", []),
            "mitigation_7d": threat.get("mitigation_7d", []),
        })

    normalized_threats.sort(key=lambda item: item.get(
        "impact_score", 0), reverse=True)

    executive_summary = report_data.get("executive_summary") or ""
    if not executive_summary:
        executive_summary = f"No high-confidence threat summary could be generated for query: {query_text}."

    key_findings = report_data.get("key_findings") or []
    if not isinstance(key_findings, list):
        key_findings = []

    recommended_actions = report_data.get("recommended_actions") or []
    if not isinstance(recommended_actions, list):
        recommended_actions = []

    source_references = report_data.get("source_references") or []
    if not isinstance(source_references, list):
        source_references = []

    if not source_references:
        source_references = [
            {
                "title": item.get("title", "No Title"),
                "url": item.get("url", "#"),
                "source_id": item.get("source_id"),
                "score": item.get("score", 0),
            }
            for item in ranked_items[:MAX_SOURCE_ITEMS]
        ]

    return {
        "report_meta": {
            "query_text": query_text,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_count": len(ranked_items),
            "threat_count": len(normalized_threats),
        },
        "executive_summary": executive_summary,
        "key_findings": key_findings,
        "ranked_threats": normalized_threats,
        "recommended_actions": recommended_actions,
        "source_references": source_references,
        "signal_quality": report_data.get("signal_quality", {}),
    }


def _generate_structured_threat_report(evidence_items: list[dict], monitor_id: str, query_text: str) -> dict | None:
    source_context = _build_source_context(evidence_items)

    prompt = f"""
You are a cybersecurity threat intelligence analyst.
Use only the provided source evidence to produce a STRICT JSON object and nothing else.

Requirements:
- Return valid JSON only.
- The response must contain: executive_summary, key_findings, ranked_threats, recommended_actions, source_references, signal_quality.
- ranked_threats must be sorted by impact_score descending.
- Every threat must include: rank, title, impact_score, confidence_score, urgency, affected_assets, attack_vector, evidence_source_ids, rationale, mitigation_now, mitigation_24h, mitigation_7d.
- Focus on actionable intelligence for humans and agents.
- If the evidence is weak or noisy, say so in executive_summary and reduce confidence scores.

Query: {query_text}
Monitor ID: {monitor_id}

Evidence:
{source_context}
"""

    raw_response = None

    if GEMINI_API_KEY:
        try:
            logger.info(
                "Attempting structured report generation with Gemini...")
            client = genai.Client(api_key=GEMINI_API_KEY,
                                  http_options={'api_version': 'v1'})
            response = client.models.generate_content(
                model='gemini-2.0-flash', contents=prompt)
            if response and response.text:
                raw_response = response.text
        except Exception as e:
            logger.warning(
                f"Gemini structured generation failed or quota exceeded: {e}. Falling back to OpenRouter...")

    if raw_response is None and OPENROUTER_API_KEY:
        try:
            logger.info(
                "Attempting structured report generation with OpenRouter (fallback)...")
            client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=OPENROUTER_API_KEY,
            )
            completion = client.chat.completions.create(
                model="google/gemini-2.0-flash-001",
                messages=[
                    {"role": "system", "content": "You are a cybersecurity threat intelligence analyst. Return strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
            )
            if completion.choices and completion.choices[0].message.content:
                raw_response = completion.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenRouter structured fallback failed: {e}")

    if not raw_response:
        logger.error(
            "All LLM providers failed to generate a structured report.")
        return None

    parsed = _extract_json_payload(raw_response)
    if not parsed:
        logger.error("LLM output was not valid JSON.")
        return None

    return _normalize_structured_report(parsed, query_text, evidence_items)


def _render_structured_report_pdf(report_content, monitor_id):
    """
    Generates a PDF from structured report content and uploads it to Supabase Storage.
    Returns the public URL.
    """
    try:
        def _pdf_safe_text(value):
            return str(value).encode('latin-1', 'replace').decode('latin-1')

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Arial", size=12)

        pdf.cell(
            200, 10, txt=_pdf_safe_text(f"Threat Report for Monitor {monitor_id}"), ln=1, align="C")
        pdf.ln(10)

        pdf.set_font("Arial", size=10)
        pdf.cell(
            200, 10, txt=_pdf_safe_text(f"Generated at: {datetime.now(timezone.utc).isoformat()}"), ln=1)
        pdf.ln(10)

        if not isinstance(report_content, dict):
            sanitized_content = _pdf_safe_text(report_content)
            pdf.multi_cell(0, 5, txt=sanitized_content)
        else:
            def write_heading(text, level=1):
                pdf.set_font("Arial", 'B', 12 if level == 1 else 10)
                pdf.multi_cell(0, 6, txt=_pdf_safe_text(text))
                pdf.set_font("Arial", '', 10)

            def write_bullets(items):
                for entry in items:
                    if isinstance(entry, dict):
                        label = entry.get("title") or entry.get(
                            "text") or entry.get("rationale") or json.dumps(entry)
                    else:
                        label = str(entry)
                    pdf.multi_cell(0, 5, txt=_pdf_safe_text(f"- {label}"))

            write_heading("Executive Summary")
            pdf.multi_cell(0, 5, txt=_pdf_safe_text(
                report_content.get("executive_summary", "")))
            pdf.ln(2)

            write_heading("Key Findings")
            key_findings = report_content.get("key_findings", [])
            if key_findings:
                write_bullets(key_findings)
            else:
                pdf.multi_cell(0, 5, txt=_pdf_safe_text(
                    "- No distinct findings identified."))
            pdf.ln(2)

            write_heading("Ranked Threats")
            ranked_threats = report_content.get("ranked_threats", [])
            if ranked_threats:
                for threat in ranked_threats:
                    if not isinstance(threat, dict):
                        continue
                    title = threat.get("title", "Unknown threat")
                    impact_score = threat.get("impact_score", 0)
                    confidence_score = threat.get("confidence_score", 0)
                    urgency = threat.get("urgency", "medium")
                    pdf.set_font("Arial", 'B', 10)
                    pdf.multi_cell(
                        0, 5, txt=_pdf_safe_text(f"{threat.get('rank', '?')}. {title} (impact {impact_score}, confidence {confidence_score}, urgency {urgency})"))
                    pdf.set_font("Arial", '', 9)
                    pdf.multi_cell(
                        0, 5, txt=_pdf_safe_text(f"Rationale: {threat.get('rationale', '')}"))
                    pdf.multi_cell(
                        0, 5, txt=_pdf_safe_text(f"Attack vector: {threat.get('attack_vector', 'unknown')}"))
                    affected_assets = threat.get("affected_assets", [])
                    if affected_assets:
                        pdf.multi_cell(
                            0, 5, txt=_pdf_safe_text(f"Affected assets: {', '.join(map(str, affected_assets))}"))
                    mitigation_now = threat.get("mitigation_now", [])
                    if mitigation_now:
                        pdf.multi_cell(0, 5, txt=_pdf_safe_text(
                            "Immediate actions:"))
                        write_bullets(mitigation_now)
                    pdf.ln(2)
            else:
                pdf.multi_cell(0, 5, txt=_pdf_safe_text(
                    "- No ranked threats identified."))

            write_heading("Recommended Actions")
            recommended_actions = report_content.get("recommended_actions", [])
            if recommended_actions:
                write_bullets(recommended_actions)
            else:
                pdf.multi_cell(
                    0, 5, txt=_pdf_safe_text("- Review sources manually and tighten monitoring query."))
            pdf.ln(2)

            write_heading("Source References")
            source_references = report_content.get("source_references", [])
            if source_references:
                for source in source_references:
                    if not isinstance(source, dict):
                        continue
                    title = source.get("title", "No Title")
                    url = source.get("url", "#")
                    score = source.get("score", 0)
                    pdf.multi_cell(
                        0, 5, txt=_pdf_safe_text(f"- {title} ({url}) [score {score}]"))
            else:
                pdf.multi_cell(0, 5, txt=_pdf_safe_text(
                    "- No source references available."))

        filename = f"report_{monitor_id}_{int(time.time())}.pdf"
        pdf_path = f"/tmp/{filename}"

        try:
            pdf.output(pdf_path)

            # Upload to Supabase Storage
            with open(pdf_path, 'rb') as f:
                supabase.storage.from_("reports").upload(
                    path=filename, file=f, file_options={"content-type": "application/pdf"})

            # Get public URL
            # Note: Bucket must be public for this to work directly as a download link
            public_url = supabase.storage.from_(
                "reports").get_public_url(filename)
            return public_url

        finally:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

    except Exception as e:
        logger.error(f"Failed to generate/upload PDF: {e}")
        return None


def _generate_pdf(report_content, monitor_id):
    return _render_structured_report_pdf(report_content, monitor_id)


class BaseTask(Task):
    """
    Base Celery Task class that handles structured logging for start, success, and failure.
    """

    def _get_log_context(self, args, kwargs):
        """
        Best-effort extraction of entity IDs for logging.
        """
        context = {}

        # Check kwargs first
        if "monitor_id" in kwargs:
            context["monitor_id"] = kwargs["monitor_id"]
        if "report_id" in kwargs:
            context["report_id"] = kwargs["report_id"]

        # Check positional args if context still empty
        # Assuming if first arg is a string, it might be an ID
        if not context and args and isinstance(args[0], str):
            # We don't know if it is monitor_id or report_id without task specific logic,
            # but usually it's monitor_id for scan_monitor and report_id for send_email.
            # However, since we want generic, we might just log it if we can infer,
            # or we rely on the fact that for our tasks, arg[0] IS the id.
            # But the requirement is not to hardcode task names.
            # So we will try to inspect the variable names of the task function if possible?
            # No, that's too complex.
            # Let's just try to be smart about it?
            # Actually, the user instruction was: "If first arg is a UUID-like string -> assume it’s the entity ID"
            # For now, let's just leave it generic or inferred by checking both?
            # Or we can just log it as a generic "id" if we are unsure?
            # But the schema requested "monitor_id" or "report_id".
            # Let's peek at the task name to hint? The user said "Do NOT hardcode: if task_name == ...".
            # BUT, we can inspect `self.run.__code__.co_varnames` if we really wanted to be magic,
            # but that's brittle.

            # Given the constraints, and "Best-effort", let's just see if we can identify it.
            # Actually, most Celery tasks calls in this codebase use positional args.
            # scan_monitor_task(monitor_id)
            # send_report_email_task(report_id)

            # Since we can't key off the name, we might not be able to distinguish "monitor_id" vs "report_id"
            # purely from a positional arg without some knowledge.
            # HOWEVER, we can just check if the key matches a pattern? No.

            # Let's try to map generic arg 0 to "entity_id" if we can't decide?
            # The prompt asked for "monitor_id (if available)" and "report_id (if available)".

            # Let's inspect the argument name of the function!
            try:
                # This works for tasks defined as functions
                arg_names = self.run.__code__.co_varnames
                if arg_names:
                    # Check if 'self' is the first argument (method bound)
                    if arg_names[0] == 'self':
                        if len(arg_names) > 1:
                            first_arg_name = arg_names[1]
                        else:
                            first_arg_name = None
                    else:
                        first_arg_name = arg_names[0]

                    if first_arg_name and first_arg_name in ['monitor_id', 'report_id']:
                        context[first_arg_name] = args[0]
            except Exception:
                pass

        return context

    def __call__(self, *args, **kwargs):
        self.start_time = time.time()

        log_payload = {
            "task": self.name,
            "status": "start",
        }
        log_payload.update(self._get_log_context(args, kwargs))

        logger.info(json.dumps(log_payload))
        return super().__call__(*args, **kwargs)

    def on_success(self, retval, task_id, args, kwargs):
        duration_ms = int((time.time() - self.start_time) * 1000)

        log_payload = {
            "task": self.name,
            "status": "success",
            "duration_ms": duration_ms,
            "result": str(retval)  # Ensure simple string representation
        }
        log_payload.update(self._get_log_context(args, kwargs))

        logger.info(json.dumps(log_payload))
        super().on_success(retval, task_id, args, kwargs)

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        duration_ms = int((time.time() - self.start_time) * 1000)

        log_payload = {
            "task": self.name,
            "status": "failure",
            "duration_ms": duration_ms,
            "error": str(exc)
        }
        log_payload.update(self._get_log_context(args, kwargs))

        logger.error(json.dumps(log_payload))
        super().on_failure(exc, task_id, args, kwargs, einfo)


def _calculate_score(item, now):
    """
    Calculates a simple score based on recency and keyword presence.
    """
    score = 0

    # 1. Recency Score
    # Google CSE returns snippet/pagemap/etc. trying to find date is tricky consistently.
    # We will look for "pagemap" -> "metatags" -> "article:published_time" or similar,
    # or rely on what's available. For MVP, we might skip complex date parsing if not readily available
    # or assign a default neutral score.
    # This is a placeholder for more robust extraction.

    # 2. Keyword Boost (Source Authority Proxy)
    text_to_scan = (item.get("title", "") + " " +
                    item.get("snippet", "")).lower()
    keywords = ["attack", "breach", "malware",
                "ransomware", "vulnerability", "exploit"]

    for kw in keywords:
        if kw in text_to_scan:
            score += 10

    # Default base score
    score += 5

    return score


def _generate_threat_report_llm(ranked_items, monitor_id, query_text):
    """
    Tries Gemini first, falls back to OpenRouter if Gemini fails (e.g. quota exhausted).
    """
    # Prepare context (top 15 items)
    articles_context = ""
    for idx, item in enumerate(ranked_items[:15], 1):
        title = item.get("title", "No Title")
        snippet = item.get("snippet", "No Snippet")
        link = item.get("link", "#")
        articles_context += f"{idx}. TITLE: {title}\n   SNIPPET: {snippet}\n   LINK: {link}\n\n"

    prompt = f"""
    You are a cybersecurity threat intelligence analyst. 
    Analyze the following search results related to the threat/monitoring query: "{query_text}".
    
    Generate a clear, actionable threat intelligence report in structured Markdown format.
    
    The report must include:
    1. **Executive Summary**: A high-level overview of the situation.
    2. **Key Findings**: Grouping of related incidents or discussions found in the articles.
    3. **Threat Analysis**: Assessment of severity, attack vectors, or trends observed.
    4. **Recommended Actions**: Specific mitigation strategies or next steps for a security team.
    5. **Source References**: Briefly list the key sources used (Titles and Links).

    If the search results are irrelevant or contain no real threats, state that clearly in the summary.

    Search Results:
    {articles_context}
    """

    # 1. Try Gemini
    if GEMINI_API_KEY:
        try:
            logger.info("Attempting report generation with Gemini...")
            client = genai.Client(api_key=GEMINI_API_KEY,
                                  http_options={'api_version': 'v1'})
            response = client.models.generate_content(
                model='gemini-2.0-flash', contents=prompt)
            if response and response.text:
                return response.text
        except Exception as e:
            logger.warning(
                f"Gemini LLM generation failed or quota exceeded: {e}. Falling back to OpenRouter...")

    # 2. Fallback to OpenRouter
    if OPENROUTER_API_KEY:
        try:
            logger.info(
                "Attempting report generation with OpenRouter (fallback)...")
            client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=OPENROUTER_API_KEY,
            )

            completion = client.chat.completions.create(
                # Using a highly available model on OpenRouter
                model="google/gemini-2.0-flash-001",
                messages=[
                    {"role": "system",
                        "content": "You are a cybersecurity threat intelligence analyst."},
                    {"role": "user", "content": prompt}
                ]
            )

            if completion.choices and completion.choices[0].message.content:
                return completion.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenRouter fallback failed: {e}")

    logger.error("All LLM providers failed to generate a report.")
    return None


@app.task(
    base=BaseTask,
    bind=True,
    name="scan_monitor_task",
    soft_time_limit=60,
    time_limit=90
)
def scan_monitor_task(self, monitor_id: str, monitor_data: dict = None):
    """
    Worker task: Scans for a monitor, generates a report, and saves it.
    Accepts optional monitor_data to avoid redundant DB fetch.
    """
    if not supabase:
        logger.error("Supabase client not initialized")
        return None

    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        logger.error("Google CSE credentials not set")
        return None

    try:
        # 1. Fetch monitor configuration
        monitor = monitor_data

        # If no data provided or missing critical fields, fetch from DB
        if not monitor or not monitor.get("query_text"):
            response = supabase.table("monitors").select(
                "*").eq("id", monitor_id).execute()
            if not response.data:
                logger.error(f"Monitor not found: {monitor_id}")
                return None
            monitor = response.data[0]

        query_text = monitor.get("query_text")

        if not query_text:
            logger.warning(f"Monitor {monitor_id} has no query_text")
            return None

        # 2. Run Google CSE Search
        service = build("customsearch", "v1",
                        developerKey=GOOGLE_CSE_API_KEY, cache_discovery=False)
        res = service.cse().list(q=query_text, cx=GOOGLE_CSE_CX, num=10).execute()
        items = res.get("items", [])

        # 3. Rank results and enrich the top sources
        ranked_items = []
        now = datetime.now(timezone.utc)
        for item in items:
            score = _calculate_score(item, now)
            item["score"] = score
            ranked_items.append(item)

        ranked_items.sort(key=lambda x: x["score"], reverse=True)

        evidence_items = []
        seen_urls = set()
        for item in ranked_items[:MAX_SOURCE_ITEMS]:
            source_url = _normalize_url(item.get("link", ""))
            if not source_url or source_url in seen_urls:
                continue
            seen_urls.add(source_url)
            source_document = _fetch_source_document(source_url)
            evidence_items.append(
                _extract_source_evidence(item, source_document))

        # 4. Generate report content (structured JSON) & PDF
        structured_report = _generate_structured_threat_report(
            evidence_items or ranked_items, monitor_id, query_text)

        if structured_report:
            report_content_for_pdf = structured_report
        else:
            report_content_for_pdf = {
                "executive_summary": "No structured report could be generated.",
                "key_findings": [],
                "ranked_threats": [
                    {
                        "rank": idx,
                        "title": item.get("title", "No Title"),
                        "impact_score": item.get("score", 0),
                        "confidence_score": 50,
                        "urgency": "medium",
                        "affected_assets": [],
                        "attack_vector": item.get("snippet", ""),
                        "evidence_source_ids": [item.get("source_id")],
                        "rationale": item.get("snippet", ""),
                        "mitigation_now": [],
                        "mitigation_24h": [],
                        "mitigation_7d": [],
                    }
                    for idx, item in enumerate(evidence_items or ranked_items[:MAX_SOURCE_ITEMS], 1)
                ],
                "recommended_actions": ["Review the top ranked sources and refine the monitor query."],
                "source_references": [
                    {
                        "title": item.get("title", "No Title"),
                        "url": item.get("url") or item.get("link", "#"),
                        "score": item.get("score", 0),
                    }
                    for item in evidence_items or ranked_items[:MAX_SOURCE_ITEMS]
                ],
                "signal_quality": {"mode": "fallback"},
            }

        pdf_url = _generate_pdf(report_content_for_pdf, monitor_id)

        # 5. Store in Supabase
        now_iso = datetime.now(timezone.utc).isoformat()

        # Insert Search Record (Pipeline)
        search_data = {
            "query_text": query_text,
            "created_at": now_iso,
            "status": "completed"
        }
        supabase.table("searches").insert(search_data).execute()

        # Insert Report Record
        item_count = len(ranked_items)
        report_data = {
            "user_id": monitor.get("user_id"),
            "monitor_id": monitor_id,
            "created_at": now_iso,
            "pdf_url": pdf_url,
            "report_json": report_content_for_pdf,
        }

        data_with_count = report_data.copy()
        data_with_count["item_count"] = item_count
        report_res = supabase.table("reports").insert(
            data_with_count).execute()

        if report_res.data:
            report_id = report_res.data[0].get("id")

            # Trigger email delivery
            send_report_email_task.delay(report_id)

            return report_id

        # Fallback if return data isn't immediate (though it usually is with explicit return)
        return "success_no_id"

    except Exception as e:
        # Re-raising ensures BaseTask.on_failure is called and the task state is set to FAILURE
        logger.error(f"Error in scan_monitor_task: {e}", exc_info=True)
        raise e


def _process_due_monitor(monitor: dict) -> dict:
    """
    Enqueues a scan task for the monitor and calculates its next_run_at.
    Designed to run in a ThreadPoolExecutor — returns the update dict for batch upsert.
    """
    monitor_id = monitor["id"]
    frequency = monitor.get("frequency", "daily").lower()
    current_next_run = datetime.fromisoformat(
        monitor["next_run_at"].replace("Z", "+00:00"))

    # Enqueue exactly once — this is the only call site for scan_monitor_task.delay
    scan_monitor_task.delay(monitor_id, monitor_data=monitor)

    try:
        next_date = calculate_next_run_at(frequency, current_next_run)
    except ValueError:
        logger.warning(
            f"Invalid frequency '{frequency}' for monitor {monitor_id}, defaulting to daily.")
        next_date = calculate_next_run_at("daily", current_next_run)

    monitor_update = monitor.copy()
    monitor_update["next_run_at"] = next_date.isoformat()
    return monitor_update


@app.task(
    base=BaseTask,
    bind=True,
    name="scan_due_monitors",
    soft_time_limit=60,
    time_limit=90
)
def scan_due_monitors(self):
    """
    Scheduler task: Finds monitors due for a run, enqueues scans, and updates next_run_at.
    Optimized to process updates in parallel using ThreadPoolExecutor.
    """
    # Kill switch logic
    if os.environ.get("DISABLE_SCHEDULER", "").lower() in ("true", "1", "yes"):
        logger.info(json.dumps({
            "task": "scan_due_monitors",
            "status": "skipped",
            "reason": "scheduler_disabled"
        }))
        return "skipped_scheduler_disabled"

    if not supabase:
        raise RuntimeError("Supabase client not initialized")

    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Query monitors
    response = supabase.table("monitors")\
        .select("*")\
        .eq("active", True)\
        .lte("next_run_at", now_iso)\
        .execute()

    monitors = response.data
    count_found = len(monitors)

    logger.info(f"Found {count_found} monitors due for scan.")

    count_enqueued = 0
    updates = []

    # 2 & 3. Enqueue tasks and calculate next_run_at in parallel.
    # _process_due_monitor is the single call site for scan_monitor_task.delay — no duplicates.
    with ThreadPoolExecutor(max_workers=min(10, count_found or 1)) as executor:
        future_to_monitor = {
            executor.submit(_process_due_monitor, monitor): monitor
            for monitor in monitors
        }
        for future in as_completed(future_to_monitor):
            monitor = future_to_monitor[future]
            try:
                update = future.result()
                updates.append(update)
                count_enqueued += 1
            except Exception as exc:
                logger.error(
                    f"Failed to process monitor {monitor.get('id')}: {exc}")

    # 4. Batch Update (Upsert)
    # Reduces N+1 write operations to a single request
    if updates:
        try:
            supabase.table("monitors").upsert(updates).execute()
        except Exception as e:
            logger.error(
                f"Failed to batch update next_run_at for monitors: {e}")
            # If batch fails, we might want to fallback or just rely on next run picking them up again.
            # Since we already enqueued the tasks, the worst case is they run again in 5 mins
            # because next_run_at wasn't updated. This is better than partial inconsistent state.

    return f"Found {count_found}, Enqueued {count_enqueued}"


@app.task(
    base=BaseTask,
    bind=True,
    name="cleanup_old_reports",
    soft_time_limit=60,
    time_limit=90
)
def cleanup_old_reports(self):
    """
    Hygiene task: Deletes reports older than RETENTION_DAYS (env var, default 30).
    """
    if not supabase:
        raise RuntimeError("Supabase client not initialized")

    try:
        retention_days = int(os.environ.get("RETENTION_DAYS", 30))
        if retention_days <= 0:
            logger.warning(
                "RETENTION_DAYS must be positive, using default of 30")
            retention_days = 30
    except ValueError:
        logger.warning("Invalid RETENTION_DAYS value, using default of 30")
        retention_days = 30
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_iso = cutoff_date.isoformat()

    response = supabase.table("reports")\
        .delete()\
        .lt("created_at", cutoff_iso)\
        .execute()

    # Supabase-py delete response format depends on version/setup,
    # but normally data contains deleted rows
    deleted_count = len(response.data) if response.data else 0

    logger.info(f"Deleted {deleted_count} old reports.")
    return f"Deleted {deleted_count} old reports"


@app.task(
    base=BaseTask,
    bind=True,
    name="send_report_email_task",
    soft_time_limit=30,
    time_limit=60
)
def send_report_email_task(self, report_id: str):
    """
    Delivery task: Sends an email notification for a generated report.
    """
    if not supabase:
        logger.error("Supabase client not initialized")
        return None

    try:
        # 1. Fetch report details
        response = supabase.table("reports").select(
            "*").eq("id", report_id).execute()
        if not response.data:
            logger.error(f"Report not found: {report_id}")
            return None

        report = response.data[0]
        user_id = report.get("user_id")
        pdf_url = report.get("pdf_url")
        # Might be None if schema doesn't match
        item_count = report.get("item_count")
        report_json = report.get("report_json") or {}

        # 2. Determine User Email
        recipient_email = None
        email_override = os.environ.get("EMAIL_OVERRIDE")

        if email_override:
            recipient_email = email_override
            logger.info(f"Using EMAIL_OVERRIDE: {recipient_email}")
        else:
            # Fetch from Supabase Auth
            try:
                user = supabase.auth.admin.get_user_by_id(user_id)
                if user and user.user and user.user.email:
                    recipient_email = user.user.email
                else:
                    logger.error(
                        f"Could not find email for user_id: {user_id}")
                    return None
            except Exception as auth_error:
                logger.error(f"Failed to fetch user email: {auth_error}")
                return None

        if not recipient_email:
            logger.error("No recipient email resolved.")
            return None

        # 3. Prepare Email Content
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = os.environ.get("SMTP_PORT")
        smtp_user = os.environ.get("SMTP_USERNAME")
        smtp_password = os.environ.get("SMTP_PASSWORD")
        email_from = os.environ.get("EMAIL_FROM")

        if not all([smtp_host, smtp_port, email_from]):
            logger.error(
                "SMTP configuration missing (HOST, PORT, or EMAIL_FROM)")
            return None

        # Construct Summary
        if isinstance(report_json, dict) and report_json.get("executive_summary"):
            summary_text = f"- {report_json.get('executive_summary')}"
        elif item_count is not None:
            summary_text = f"- {item_count} potential threats identified"
        else:
            summary_text = "- Potential threats identified"

        subject = "Your ThreatWatch report is ready"
        body = f"""Your ThreatWatch report has been generated successfully.

Summary:
{summary_text}

You can download the full report here:
{pdf_url}

— ThreatWatch
"""

        msg = MIMEMultipart()
        msg['From'] = email_from
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        # 4. Send Email
        try:
            port = int(smtp_port)
            server = smtplib.SMTP(smtp_host, port)
            server.starttls()
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)

            server.sendmail(email_from, recipient_email, msg.as_string())
            server.quit()

            logger.info(
                f"Email sent to {recipient_email} for report {report_id}")
            return "email_sent"

        except Exception as smtp_error:
            logger.error(f"SMTP error: {smtp_error}")
            raise smtp_error

    except Exception as e:
        logger.error(f"Error in send_report_email_task: {e}", exc_info=True)
        raise e
