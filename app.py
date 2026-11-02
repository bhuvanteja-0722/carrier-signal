#!/usr/bin/env python3
"""Career Signal: a small job-market career guide and MCP server."""
import json
import html
import os
import re
import secrets
import hashlib
import hmac
import sqlite3
from contextlib import contextmanager
import ssl
import sys
import threading
import time
import http.cookies
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
# Some macOS Python distributions do not point at the system certificate bundle.
if not os.getenv("SSL_CERT_FILE") and Path("/etc/ssl/cert.pem").exists():
    os.environ["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"
SOURCE_REGISTRY_PATH = ROOT / "employer_sources.json"
SOURCE_REGISTRY = json.loads(SOURCE_REGISTRY_PATH.read_text(encoding="utf-8")) if SOURCE_REGISTRY_PATH.exists() else {"companies": []}
MAJOR_EMPLOYERS_PATH = ROOT / "major_employers.json"
MAJOR_EMPLOYERS = json.loads(MAJOR_EMPLOYERS_PATH.read_text(encoding="utf-8")) if MAJOR_EMPLOYERS_PATH.exists() else {"companies":[]}
LIVE_VENDORS = {"greenhouse","lever","ashby"}
LIVE_COMPANIES = SOURCE_REGISTRY.get("companies",[])
LIVE_NAMES = {x["name"].casefold() for x in LIVE_COMPANIES}
DIRECTORY_COMPANIES = LIVE_COMPANIES + [{**x,"vendor":"external","company_type":"Multinational"} for x in MAJOR_EMPLOYERS.get("companies",[]) if x["name"].casefold() not in LIVE_NAMES]
BOARD_CACHE = {}
BOARD_CACHE_LOCK = threading.Lock()
SCAN_NEXT_INDEX = {}
SCAN_LOCK = threading.Lock()
BOARD_CACHE_TTL = 15 * 60
BOARD_CACHE_TTL_SECONDS = BOARD_CACHE_TTL
BOARD_REQUEST_TIMEOUT = 3
BOARD_MAX_RESPONSE_BYTES = 2_500_000
BOARD_WORKERS = 16
DATA_DIR = Path(os.getenv("CAREER_SIGNAL_DATA_DIR", str(ROOT))).expanduser()
TREND_HISTORY_PATH = DATA_DIR / "market_history.json"
TREND_HISTORY_LOCK = threading.Lock()
ACCOUNT_DB_PATH = Path(os.getenv("CAREER_SIGNAL_DB_PATH", str(DATA_DIR / "career_signal_accounts.sqlite3"))).expanduser()
SESSION_COOKIE = "career_signal_session"
SESSION_DAYS = 14
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0").casefold() in {"1", "true", "yes"}
AUTH_RATE_WINDOW = 15 * 60
AUTH_RATE_MAX = 10
AUTH_RATE_LOCK = threading.Lock()
AUTH_RATE_ATTEMPTS = {}
SKILLS = ["python", "sql", "excel", "tableau", "power bi", "machine learning", "statistics", "communication", "data visualization", "aws", "azure", "javascript", "react", "figma", "user research", "project management", "leadership", "salesforce", "financial modeling", "accounting", "testing", "docker", "kubernetes", "linux", "git", "prompt engineering", "generative ai", "communication skills", "problem solving", "stakeholder management", "agile", "product management", "marketing", "seo", "copywriting", "customer service", "java", "data structures", "algorithms", "rest api", "unit testing", "object oriented programming", "spring boot", "node.js", "typescript"]
SKILLS = list(dict.fromkeys(SKILLS + ["business analysis","business intelligence","data analysis","data modeling","data engineering","data pipelines","data governance","data quality","data mining","data storytelling","reporting","dashboards","kpi reporting","financial analysis","forecasting","budgeting","risk management","compliance","audit","pricing","market research","customer analytics","people analytics","product analytics","experimentation","a/b testing","sql server","mysql","postgresql","mongodb","snowflake","bigquery","redshift","databricks","apache spark","airflow","dbt","looker","powerpoint","microsoft office","google analytics","google sheets","vba","r programming","scala","c++","c#","go","ruby","php","swift","kotlin","rest apis","graphql","system design","microservices","cloud computing","google cloud platform","gcp","terraform","ci/cd","jenkins","github actions","security","cybersecurity","networking","machine learning operations","deep learning","natural language processing","llm","large language models","generative ai","prompt engineering","jira","confluence","user stories","agile methodology","scrum","six sigma","supply chain","operations research","sales operations","crm","communication","written communication","presentation skills","cross-functional collaboration","strategic planning","vendor management","business process improvement"]))
SKILL_LABELS = {"sql":"SQL","aws":"AWS","azure":"Azure","javascript":"JavaScript","power bi":"Power BI","node.js":"Node.js","rest api":"REST API","rest apis":"REST APIs","ci/cd":"CI/CD","c++":"C++","c#":"C#","llm":"LLM","gcp":"GCP","vba":"VBA","a/b testing":"A/B testing","kpi reporting":"KPI reporting","google cloud platform":"Google Cloud Platform","apache spark":"Apache Spark","r programming":"R programming"}

def board_endpoint(source):
    slug = urllib.parse.quote(source["slug"], safe="-_.~")
    vendor = source["vendor"]
    if vendor == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    if vendor == "lever":
        return f"https://api.lever.co/v0/postings/{slug}?mode=json"
    return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"

def normalize_board_jobs(source, payload):
    vendor = source["vendor"]
    raw_jobs = payload if isinstance(payload, list) else payload.get("jobs", [])
    jobs = []
    for item in raw_jobs:
        if vendor == "greenhouse":
            offices = item.get("offices") or []
            place = item.get("location") or {}
            location = place.get("name", "") if isinstance(place, dict) else str(place)
            if not location:
                location = ", ".join(x.get("name", "") for x in offices if x.get("name"))
            description = item.get("content", "")
            url = item.get("absolute_url", "")
            created = item.get("updated_at", "")
            date_label = "Updated"
            salary_min = salary_max = None
            currency = None
            team = ", ".join(x.get("name","") for x in (item.get("departments") or []) if x.get("name"))
            employment_type = item.get("employment_type","")
        elif vendor == "lever":
            cats = item.get("categories") or {}
            location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
            team = cats.get("team") or cats.get("department") or ""
            employment_type = cats.get("commitment") or ""
            description = item.get("descriptionPlain") or item.get("description") or ""
            url = item.get("hostedUrl", "")
            created = datetime.fromtimestamp(item["createdAt"] / 1000, timezone.utc).isoformat() if item.get("createdAt") else ""
            date_label = "Posted"
            salary = item.get("salaryRange") or {}
            salary_min, salary_max = salary.get("min"), salary.get("max")
            currency = salary.get("currency")
        else:
            team = item.get("departmentName") or item.get("teamName") or ""
            employment_type = item.get("employmentType") or ""
            location = item.get("location", "")
            address = item.get("address") or {}
            postal = address.get("postalAddress") or {}
            location = location or ", ".join(x for x in [postal.get("addressLocality"), postal.get("addressRegion"), postal.get("addressCountry")] if x)
            description = item.get("descriptionPlain") or item.get("descriptionHtml") or ""
            url = item.get("jobUrl") or item.get("applyUrl", "")
            created = item.get("publishedAt", "")
            date_label = "Published"
            salary_min = salary_max = None
            currency = None
        jobs.append({"title":item.get("title", "Role"),"company":source["name"],"location":location or "Location not listed","description":description,"url":url,"created":created,"date_label":date_label,"salary_min":salary_min,"salary_max":salary_max,"currency":currency,"team":team,"employment_type":employment_type,"board":source["careers_url"],"provider":vendor.title()})
    return jobs

def fetch_board(source, force=False, role=None):
    query_key = (role or "").strip().casefold()
    key = (source["vendor"], source["slug"], query_key)
    now = time.monotonic()
    with BOARD_CACHE_LOCK:
        cached = BOARD_CACHE.get(key)
        cached_ttl = BOARD_CACHE_TTL_SECONDS if cached and cached.get("ok") else 120
        if cached and not force and now - cached["cached_at"] < cached_ttl:
            return cached
    request = urllib.request.Request(board_endpoint(source), headers={"Accept":"application/json","User-Agent":"CareerSignal/1.0 (public employer job-board reader)"})
    try:
        with urllib.request.urlopen(request, timeout=BOARD_REQUEST_TIMEOUT) as response:
            body = response.read(BOARD_MAX_RESPONSE_BYTES + 1)
        if len(body) > BOARD_MAX_RESPONSE_BYTES:
            raise ValueError("Employer feed exceeds the response-size limit")
        payload = json.loads(body.decode("utf-8"))
        jobs = normalize_board_jobs(source, payload)
        if role:
            jobs = [job for job in jobs if role_matches(job, role)]
        result = {"ok":True,"jobs":jobs,"cached_at":time.monotonic(),"checked_at":datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        result = {"ok":False,"jobs":[],"cached_at":time.monotonic(),"checked_at":datetime.now(timezone.utc).isoformat(),"error":str(exc)[:180]}
    with BOARD_CACHE_LOCK:
        if len(BOARD_CACHE) >= 4000:
            BOARD_CACHE.pop(next(iter(BOARD_CACHE)))
        BOARD_CACHE[key] = result
    return result

COUNTRY_NAMES = {"in":["india"],"us":["united states","usa","u.s."],"gb":["united kingdom","uk","great britain"],"ca":["canada"],"au":["australia"],"de":["germany"],"fr":["france"],"nl":["netherlands"],"sg":["singapore"],"za":["south africa"]}
ROLE_ALIASES = {"sde":["software engineer","software development engineer","software developer","backend engineer"],"data analyst":["data analyst","analytics associate","business intelligence analyst","insights analyst"]}

def plain_text(value):
    value=html.unescape(value or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()

def requirement_blocks(description):
    source=html.unescape(description or "")
    chunks=re.split(r"</(?:li|p|div|h[1-6]|tr|td)>|<br\s*/?>|[\r\n]+",source,flags=re.I)
    return [plain_text(chunk) for chunk in chunks if plain_text(chunk)]

def extract_skill_evidence(jobs,limit=12):
    """Return job-description skill mentions with requirement labels only where wording is explicit."""
    if not jobs: return []
    signals={}
    patterns=[(skill,re.compile(r"(?<![a-z0-9])"+re.escape(skill)+r"(?![a-z0-9])",re.I)) for skill in SKILLS]
    preferred_re=re.compile(r"\b(preferred|nice[\s-]+to[\s-]+have|desirable|bonus|a plus|would be a plus|good to have)\b",re.I)
    required_re=re.compile(r"\b(required|must have|minimum qualifications?|basic qualifications?|essential skills?|proficiency in|proficient in|experience with|knowledge of)\b",re.I)
    reset_re=re.compile(r"\b(responsibilities|what you.ll do|about the role|about us|benefits|our culture)\b",re.I)
    for job in jobs:
        blocks=[plain_text(job.get("title",""))]+requirement_blocks(job.get("description",""))
        section=None
        job_mentions={}
        for block in blocks:
            low=block.casefold()
            if preferred_re.search(low) and len(block)<180: section="preferred"
            elif required_re.search(low) and len(block)<180: section="required"
            elif reset_re.search(low) and len(block)<180: section=None
            for skill,pattern in patterns:
                match=pattern.search(block)
                if not match: continue
                context=block[max(0,match.start()-100):min(len(block),match.end()+100)]
                explicit="preferred" if preferred_re.search(context) else "required" if required_re.search(context) else section or "mentioned"
                item=job_mentions.setdefault(skill,{"signals":set(),"evidence":[]})
                item["signals"].add(explicit)
                if len(item["evidence"])<2 and len(block)>18:
                    left=max(block.rfind(".",0,match.start()),block.rfind(";",0,match.start()),block.rfind(":",0,match.start()))+1
                    right_candidates=[p for p in (block.find(".",match.end()),block.find(";",match.end())) if p>=0]
                    right=min(right_candidates) + 1 if right_candidates else min(len(block),match.end()+130)
                    snippet=block[left:right].strip()
                    if len(snippet)>230: snippet=snippet[:227].rsplit(" ",1)[0]+"…"
                    item["evidence"].append({"title":job.get("title","Role"),"signal":explicit,"text":snippet or block[max(0,match.start()-75):match.end()+90]})
        for skill,item in job_mentions.items():
            agg=signals.setdefault(skill,{"skill":SKILL_LABELS.get(skill,skill.title()),"count":0,"required_count":0,"preferred_count":0,"mentioned_count":0,"evidence":[]})
            agg["count"]+=1
            if "required" in item["signals"]: agg["required_count"]+=1
            if "preferred" in item["signals"]: agg["preferred_count"]+=1
            if "mentioned" in item["signals"]: agg["mentioned_count"]+=1
            for evidence in item["evidence"]:
                if len(agg["evidence"])<3: agg["evidence"].append(evidence)
    ranked=sorted(signals.values(),key=lambda x:(-x["count"],-x["required_count"],-x["preferred_count"],x["skill"].casefold()))
    return ranked[:limit]

def salary_summary(jobs):
    rows=[j for j in jobs if j.get("currency") and any(isinstance(j.get(k), (int,float)) and j[k]>0 for k in ("salary_min","salary_max"))]
    currencies={j["currency"] for j in rows}
    if not rows:
        return {"kind":"undisclosed","label":"No employer-disclosed pay in matching listings","currency":None,"min":None,"max":None,"samples":0}
    if len(currencies)!=1:
        return {"kind":"mixed","label":"Pay disclosed in mixed currencies","currency":None,"min":None,"max":None,"samples":len(rows)}
    cur=next(iter(currencies)); lows=[j["salary_min"] for j in rows if isinstance(j.get("salary_min"),(int,float)) and j["salary_min"]>0]; highs=[j["salary_max"] for j in rows if isinstance(j.get("salary_max"),(int,float)) and j["salary_max"]>0]
    lo=min(lows) if lows else None; hi=max(highs) if highs else None
    def money(n): return ("₹" if cur.upper() in ("INR","INR") else cur.upper()+" ")+format(n/100000 if cur.upper()=="INR" else n/1000 if cur.upper()=="USD" else n,".1f")+("L" if cur.upper()=="INR" else "k" if cur.upper()=="USD" else "")
    label=(money(lo) if lo is not None else "")+("–"+money(hi) if hi is not None and hi!=lo else "")
    return {"kind":"employer_disclosed","label":label,"currency":cur,"min":lo,"max":hi,"samples":len(rows)}

def role_level(title):
    t=title.casefold()
    if re.search(r"\b(intern|junior|entry|associate|i|1)\b",t): return 1
    if re.search(r"\b(staff|principal|director|head|vp|vice president|chief)\b",t): return 4
    if re.search(r"\b(senior|lead|manager|ii|iii|iv|2|3|4)\b",t): return 3
    return 2

def company_role_progression(jobs):
    titles=sorted({j.get("title","") for j in jobs if j.get("title")},key=lambda x:(role_level(x),x.casefold()))
    if not titles: return {"label":"No current matching roles","titles":[],"note":"Progression cannot be inferred from this search."}
    levels={role_level(t) for t in titles}
    observed={"label":"Multiple levels visible in current postings","titles":titles[:5],"note":"Observed titles only; this does not confirm internal promotion routes or timelines."} if len(levels)>1 else {"label":"Only one level visible in current postings","titles":titles[:3],"note":"Promotion expectations and timelines are not published in these feeds."}
    observed["market_path"]="Senior-level specialist, then lead or manager; the route depends on the team and discipline." if any(x in " ".join(titles).casefold() for x in ("analyst","analytics")) else "Senior-level specialist, then lead or manager; the route depends on the team and discipline."
    return observed

def role_brief(role, jobs, counts):
    action_words=["analyze","build","develop","design","create","support","manage","lead","collaborate","communicate","maintain","deliver","identify","present","monitor","research","improve","implement","own","partner","report","test","write","automate"]
    sentence_hits={}
    relevance_words={"data","analytics","analysis","sql","python","excel","dashboard","reporting","metrics","insight","business","visualization","statistical","model","stakeholder","forecast","experiment","database"}
    for job in jobs:
        text=plain_text(job.get("description",""))
        for sentence in re.split(r"(?<=[.!?])\s+",text):
            s=sentence.strip()
            words=set(re.findall(r"[a-z]+",s.casefold()))
            if 35<=len(s)<=260 and len(words & relevance_words)>=1 and any(re.search(r"\b"+verb+r"\w*\b",s,re.I) for verb in action_words):
                key=re.sub(r"[^a-z0-9 ]","",s.casefold())
                sentence_hits.setdefault(key,{"text":s,"count":0})["count"]+=1
    responsibilities=sorted(sentence_hits.values(),key=lambda x:(-x["count"],len(x["text"])))[:3]
    return {"overview":f"{role} openings currently emphasize {', '.join(x['skill'] for x in counts[:3]) or 'the requirements described by employers'}. This summary is based on live matching postings across the selected market.","responsibilities":[x["text"] for x in responsibilities],"responsibility_note":"Themes from employer descriptions; wording varies by company." if responsibilities else "Employer descriptions did not provide enough role-specific detail to summarize responsibilities reliably.","progression":"Common market path: analyst → senior analyst → lead or analytics manager. This is a broad career pattern, not a promise of promotion or a company-specific policy; timelines vary." if re.search(r"analyst|analytics",role,re.I) else "Common market path: individual contributor → senior specialist → lead or manager. This is a broad career pattern, not a promise of promotion or a company-specific policy; timelines vary."}

@contextmanager
def account_db():
    con=sqlite3.connect(ACCOUNT_DB_PATH,timeout=10)
    con.row_factory=sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def init_account_db():
    with account_db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, salt BLOB NOT NULL, password_hash BLOB NOT NULL, created_at TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires_at INTEGER NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'Data Analyst', location TEXT NOT NULL DEFAULT 'India', country TEXT NOT NULL DEFAULT 'in', skills_json TEXT NOT NULL DEFAULT '[]', saved_companies_json TEXT NOT NULL DEFAULT '[]', search_history_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL)")
        con.execute("PRAGMA foreign_keys=ON")

def password_digest(password,salt):
    return hashlib.pbkdf2_hmac("sha256",password.encode(),salt,310_000)

def safe_profile(row):
    return {"role":row["role"],"location":row["location"],"country":row["country"],"skills":json.loads(row["skills_json"]),"saved_companies":json.loads(row["saved_companies_json"]),"search_history":json.loads(row["search_history_json"]),"updated_at":row["updated_at"]}

def role_matches(job, role):
    title = (job.get("title", "")).casefold()
    text = (title + " " + re.sub(r"<[^>]+>", " ", job.get("description", ""))).casefold()
    normalized = re.sub(r"[^a-z0-9 ]", " ", role.casefold())
    aliases = next((v for k, v in ROLE_ALIASES.items() if normalized.strip() == k or normalized.strip().startswith(k + " ")), [])
    if aliases:
        return any(alias in title for alias in aliases)
    role_phrase = re.sub(r"\s+", " ", normalized).strip()
    if role_phrase and role_phrase in re.sub(r"\s+", " ", title):
        return True
    terms = [x for x in role_phrase.split() if len(x) > 2 and x not in {"senior","junior","entry","level","associate","the","and"}]
    return bool(terms) and all(term in text for term in terms)

def location_matches(job, location, country):
    if not location or location.casefold() in {"anywhere", "global", "worldwide"}:
        return True
    place = job.get("location", "").casefold()
    city_aliases = {"in":["bengaluru","bangalore","hyderabad","chennai","mumbai","pune","delhi","gurugram","gurgaon","noida","india"],"us":["new york","san francisco","seattle","united states","usa","u.s."],"gb":["london","united kingdom","uk"]}
    terms = city_aliases.get(country, [])
    if any(term in place for term in terms):
        return True
    if "remote" in place and not any(country_name in place for names in COUNTRY_NAMES.values() for country_name in names):
        return True
    return location.casefold() in place

def search_jobs(role="Data Analyst", location="India", country="in", page=1, results=50, force=False):
    sources = SOURCE_REGISTRY.get("companies", [])
    role_key = (role or "").strip().casefold()
    if not sources:
        raise RuntimeError("No live employer feeds are configured.")
    with SCAN_LOCK:
        if force:
            with BOARD_CACHE_LOCK:
                for key in [key for key in BOARD_CACHE if len(key) > 2 and key[2] == role_key]:
                    BOARD_CACHE.pop(key, None)
            SCAN_NEXT_INDEX[role_key] = 0
        start = SCAN_NEXT_INDEX.get(role_key, 0)
        if start >= len(sources):
            start = 0
        end = min(len(sources), start + BOARD_WORKERS)
        batch = sources[start:end]
        SCAN_NEXT_INDEX[role_key] = end
        # Fetch one bounded group per request. The browser requests the next group while
        # the progress indicator stays visible, keeping the free instance responsive.
        with ThreadPoolExecutor(max_workers=BOARD_WORKERS) as pool:
            futures = [pool.submit(fetch_board, source, force, role) for source in batch]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass
    cache_now = time.monotonic()
    gathered = []
    good_sources = set()
    feed_times = []
    checked_sources = set()
    with BOARD_CACHE_LOCK:
        cached_feeds = {key: value for key, value in BOARD_CACHE.items() if len(key) > 2 and key[2] == role_key and cache_now - value["cached_at"] < (BOARD_CACHE_TTL_SECONDS if value.get("ok") else 120)}
    for source in sources:
        key = (source["vendor"], source["slug"], role_key)
        feed = cached_feeds.get(key)
        if feed is None:
            continue
        checked_sources.add((source["vendor"], source["slug"]))
        if not feed["ok"]:
            continue
        good_sources.add((source["vendor"], source["slug"]))
        feed_times.append(feed["checked_at"])
        gathered.extend(feed["jobs"])
    checked_count = len(checked_sources)
    failures = checked_count - len(good_sources)
    scan_complete = checked_count >= len(sources)
    matches = [job for job in gathered if role_matches(job, role) and location_matches(job, location, country)]
    unique = {}
    for job in matches:
        key = (job.get("url") or "").casefold() or (job["company"].casefold(), job["title"].casefold(), job["location"].casefold())
        unique[key] = job
    matches = list(unique.values())
    matches.sort(key=lambda j: j.get("created", ""), reverse=True)
    checked_at = datetime.now(timezone.utc).isoformat()
    note = f"Live postings read from {len(good_sources)} of {len(sources)} employer boards ({checked_count}/{len(sources)} checked). Links open the employer's hosted posting."
    if not matches:
        note = (f"No open {role} postings matched {location} yet; the live scan is still running." if not scan_complete else f"No open {role} postings matched {location} across {len(good_sources)} reachable employer boards. Try another role or location.")
    if failures:
        note += f" {failures} checked feeds could not be reached."
    if not scan_complete:
        note += " Checking the next group automatically."
    feed_updated_at=min(feed_times) if feed_times else checked_at
    return {"jobs":matches[:results],"_analysis_jobs":matches,"total":len(matches),"source":"employer_boards","source_count":len(sources),"boards_checked":len(good_sources),"scan_checked":checked_count,"scan_complete":scan_complete,"updated_at":feed_updated_at,"notice":note}

def analyze_market(role="Data Analyst", location="India", country="in", user_skills=None, weeks=6, force=False):
    result=search_jobs(role,location,country,results=12,force=force)
    jobs=result.pop("_analysis_jobs", result["jobs"])
    corpus=" ".join((j.get("title","")+" "+j.get("description","")) for j in jobs).lower()
    counts=[]
    for skill in SKILLS:
        n=sum(1 for j in jobs if re.search(r"(?<![\w])"+re.escape(skill)+r"(?![\w])",(j.get("title","")+" "+j.get("description","")).lower()))
        if n: counts.append({"skill":SKILL_LABELS.get(skill,skill.title()),"count":n,"share":round(100*n/max(1,len(jobs)))})
    role_key = "sde" if re.search(r"\bsde\b|software (development )?(engineer|developer)|backend engineer", role, re.I) else "data analyst" if re.search(r"data analyst|business intelligence", role, re.I) else ""
    priorities = {"sde":["java","python","javascript","data structures","algorithms","rest api","spring boot","git"],"data analyst":["sql","python","excel","power bi","tableau","statistics","data visualization"]}.get(role_key,[])
    priority_rank={skill:i for i,skill in enumerate(priorities)}
    counts.sort(key=lambda x:(-x["count"],priority_rank.get(x["skill"].lower(),999),x["skill"]))
    if isinstance(user_skills, str):
        user_skills = [part.strip() for part in user_skills.split(",") if part.strip()]
    skills=[x["skill"].lower() if isinstance(x, dict) else str(x).lower() for x in (user_skills or [])]
    gaps=[x for x in counts if x["skill"].lower() not in skills]
    role_label=role.strip() or "your target role"
    roadmap=[]
    for i,item in enumerate(gaps[:6]):
        roadmap.append({"week":min(weeks,1+i*max(1,weeks//6)),"skill":item["skill"],"why":f"Appears in {item['count']} of {len(jobs)} listings in this search ({item['share']}%).","project":project_for(item["skill"],role_label)})
    salary_market=salary_summary(jobs)
    if salary_market["samples"] < 3:
        salary_market={"kind":"insufficient","label":"Not enough disclosed salary data for a market estimate","currency":None,"min":None,"max":None,"samples":salary_market["samples"]}
    else:
        salary_market["kind"]="market_estimate"
        salary_market["label"] += " estimated from employer-listed ranges"
    result["jobs"] = [{**{k:v for k,v in job.items() if k != "description"},"skill_evidence":extract_skill_evidence([job],8)} for job in result["jobs"]]
    jobs_by_company={}
    for job in jobs:
        public_job={**{k:v for k,v in job.items() if k != "description"},"skill_evidence":extract_skill_evidence([job],8)}
        jobs_by_company.setdefault(job["company"],[]).append(public_job)
    company_pages=[]
    for source in sorted(DIRECTORY_COMPANIES,key=lambda x:x["name"].casefold()):
        company_jobs=jobs_by_company.get(source["name"],[])
        company_full=[j for j in jobs if j["company"]==source["name"]]
        company_skill_evidence=extract_skill_evidence(company_full,12)
        company_skill_counts=[{"skill":x["skill"],"count":x["count"]} for x in company_skill_evidence]
        layoffs_q=urllib.parse.urlencode({"q":f'"{source["name"]}" layoffs'})
        linkedin_company_q=urllib.parse.urlencode({"keywords":source["name"]})
        active_locations=sorted({j.get("location","") for j in company_full if j.get("location") and j.get("location")!="Location not listed"})
        li_location=active_locations[0] if active_locations else location
        linkedin_jobs_q=urllib.parse.urlencode({"keywords":source["name"],"location":li_location})
        teams=sorted({j.get("team","") for j in company_full if j.get("team")})
        types=sorted({j.get("employment_type","") for j in company_full if j.get("employment_type")})
        company_pages.append({"name":source["name"],"provider":"Official career site" if source["vendor"]=="external" else source["vendor"].title(),"company_type":source.get("company_type","Employer"),"sector":source.get("sector",""),"career_site_only":source["vendor"]=="external","careers_url":source["careers_url"],"matching_count":len(company_full),"jobs":company_jobs[:6],"locations":active_locations[:8],"linkedin_market_location":li_location,"linkedin_company_search_url":"https://www.linkedin.com/search/results/companies/?"+linkedin_company_q,"linkedin_jobs_url":"https://www.linkedin.com/jobs/search/?"+linkedin_jobs_q,"hiring_teams":teams[:6],"employment_types":types[:4],"pay":salary_summary(company_full),"skills":company_skill_counts[:5],"skill_evidence":company_skill_evidence,"skill_evidence_status":"live_postings" if company_full else "no_live_postings","progression":company_role_progression(company_full),"layoff_url":"https://news.google.com/search?"+layoffs_q})
    trend_note=record_market_snapshot(role,location,country,result.get("total",len(jobs)),counts)
    return {**result,"role":role,"location":location,"job_count":result.get("total",len(jobs)),"skills":counts[:10],"gaps":gaps[:8],"roadmap":roadmap,"trend_note":trend_note,"company_pages":company_pages,"salary_market":salary_market,"role_brief":role_brief(role,jobs,counts)}

def project_for(skill,role):
    projects={"sql":f"Build a small database project for a {role} use case; write joins, aggregations and window queries.","python":f"Create a Python analysis notebook for a realistic {role} problem and explain the result.","power bi":"Build an interactive dashboard with a clear KPI summary and drill-downs.","excel":"Create a clean spreadsheet analysis with formulas, pivot tables and a one-page summary.","tableau":"Design a dashboard that answers three business questions from a public dataset.","statistics":"Run an experiment or statistical analysis; state assumptions and limitations.","communication":f"Turn a {role} analysis into a one-page brief with a chart and recommendation.","data visualization":"Create three charts for one dataset and explain which chart best answers each question.","machine learning":"Train a simple baseline model, measure it, and document where it can fail.","user research":"Plan five user interviews, synthesize themes and propose a product change.","react":"Build a small responsive interface that solves a role-relevant workflow.","aws":"Deploy a small project to a cloud service and document its architecture.","git":"Publish a role-relevant portfolio project with a clear README and version history.","java":"Build a small Java service with clean classes, error handling and a short README.","data structures":"Implement arrays, hash maps, stacks and trees; explain time and space complexity.","algorithms":"Solve role-level coding problems and document the approach, complexity and edge cases.","rest api":"Build and document a REST API with validation, error responses and a few tests.","unit testing":"Add focused unit tests for a small service and explain what behavior each test protects.","object oriented programming":"Model a small domain with clear classes and interfaces; explain the design trade-offs.","spring boot":"Build a Spring Boot CRUD API with validation and a SQL-backed data model."}
    return projects.get(skill.lower(),f"Complete a small portfolio task using {skill}, then write up your approach and results for a {role} audience.")

def record_market_snapshot(role, location, country, total, counts):
    """Keep one aggregate-only snapshot per day to compare later searches honestly."""
    key = "|".join(re.sub(r"\s+", " ", x.casefold().strip()) for x in (role, location, country))
    now = datetime.now(timezone.utc)
    with TREND_HISTORY_LOCK:
        try:
            history = json.loads(TREND_HISTORY_PATH.read_text(encoding="utf-8")) if TREND_HISTORY_PATH.exists() else {}
        except (OSError, json.JSONDecodeError):
            history = {}
        snapshots = history.get(key, [])
        prior = next((s for s in reversed(snapshots) if (now - datetime.fromisoformat(s["updated_at"])).total_seconds() >= 12 * 3600), None)
        snapshot = {"updated_at":now.isoformat(),"total":total,"skills":{x["skill"]:x["count"] for x in counts}}
        if snapshots and now.date() == datetime.fromisoformat(snapshots[-1]["updated_at"]).date():
            snapshots[-1] = snapshot
        else:
            snapshots.append(snapshot)
        history[key] = snapshots[-180:]
        tmp = TREND_HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(history,ensure_ascii=False),encoding="utf-8")
        tmp.replace(TREND_HISTORY_PATH)
    if not prior:
        return "Trend baseline saved. Search again tomorrow to compare availability and skill signals over time."
    prior_at = datetime.fromisoformat(prior["updated_at"])
    hours = max(1,round((now-prior_at).total_seconds()/3600))
    delta = total - int(prior.get("total",0))
    direction = "more" if delta > 0 else "fewer" if delta < 0 else "the same number of"
    changes=[]
    current={x["skill"]:x["count"] for x in counts}
    for skill in set(current) | set(prior.get("skills",{})):
        change=current.get(skill,0)-prior.get("skills",{}).get(skill,0)
        if change: changes.append((abs(change),skill,change))
    changes.sort(reverse=True)
    skill_text=""
    if changes:
        _,skill,change=changes[0]
        skill_text=f" {skill} mentions {'increased' if change>0 else 'decreased'} by {abs(change)}."
    return f"Compared with the saved snapshot {hours} hours ago: {abs(delta)} {direction} matching listings.{skill_text}"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=="/api/auth":
            user=self.current_user()
            if not user: return self.send_json({"authenticated":False})
            return self.send_json({"authenticated":True,"email":user["email"],"profile":self.get_profile(user["id"])})
        if self.path=="/api/profile":
            user=self.current_user()
            if not user: return self.send_json({"error":"Sign in to load your saved profile."},status=401)
            return self.send_json(self.get_profile(user["id"]))
        if self.path=="/api/health": return self.send_json({"ok":True,"live_configured":bool(LIVE_COMPANIES),"source_count":len(LIVE_COMPANIES),"directory_count":len(DIRECTORY_COMPANIES)})
        if self.path=="/api/sources":
            public={"count":len(DIRECTORY_COMPANIES),"live_board_count":len(LIVE_COMPANIES),"verified_at":SOURCE_REGISTRY.get("verified_at"),"catalog_source":SOURCE_REGISTRY.get("source"),"license":SOURCE_REGISTRY.get("license"),"companies":[{"name":s["name"],"provider":"Official career site" if s["vendor"]=="external" else s["vendor"].title(),"live_supported":s["vendor"] in LIVE_VENDORS,"sector":s.get("sector",""),"careers_url":s["careers_url"]} for s in DIRECTORY_COMPANIES]}
            return self.send_json(public)
        if self.path.startswith("/api/analyze"):
            try:
                q=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                result=analyze_market(q.get("role",["Data Analyst"])[0],q.get("location",["India"])[0],q.get("country",["in"])[0],q.get("skills",[""])[0].split(","),int(q.get("weeks",[6])[0]),q.get("force",["0"])[0]=="1")
                return self.send_json(result)
            except Exception as exc:
                return self.send_json({"error":str(exc),"hint":"Check API credentials, country code, and connection."})
        if self.path=="/" or self.path=="/index.html":
            data=(ROOT/"index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","strict-origin-when-cross-origin"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
        if self.path=="/style.css" or self.path=="/dark.css" or self.path=="/app.js" or self.path=="/theme.js":
            name=self.path[1:]; data=(ROOT/name).read_bytes(); self.send_response(200); self.send_header("Content-Type","text/css; charset=utf-8" if name.endswith("css") else "application/javascript; charset=utf-8"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","strict-origin-when-cross-origin"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_error(404)
    def do_POST(self):
        try:
            origin=self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc.lower()!=self.headers.get("Host","").lower():
                return self.send_json({"error":"Request origin is not allowed."},status=403)
            length=int(self.headers.get("Content-Length","0"))
            if length<0 or length>1_000_000: return self.send_json({"error":"Request is too large."},status=413)
            payload=json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload,dict): return self.send_json({"error":"Invalid request."},status=400)
            if self.path=="/api/auth/register": return self.register_account(payload)
            if self.path=="/api/auth/login": return self.login_account(payload)
            if self.path=="/api/auth/logout": return self.logout_account()
            if self.path=="/api/profile": return self.save_profile(payload)
            return self.send_error(404)
        except (ValueError,sqlite3.Error) as exc:
            return self.send_json({"error":str(exc) if isinstance(exc,ValueError) else "Could not save account data."},status=400)
    def current_user(self):
        cookies=http.cookies.SimpleCookie()
        try: cookies.load(self.headers.get("Cookie",""))
        except http.cookies.CookieError: return None
        morsel=cookies.get(SESSION_COOKIE)
        if not morsel or not morsel.value: return None
        token_hash=hashlib.sha256(morsel.value.encode()).hexdigest()
        with account_db() as con:
            row=con.execute("SELECT users.id,users.email,sessions.expires_at FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token_hash=?",(token_hash,)).fetchone()
            if not row: return None
            if row["expires_at"]<int(time.time()):
                con.execute("DELETE FROM sessions WHERE token_hash=?",(token_hash,)); return None
            return row
    def get_profile(self,user_id):
        with account_db() as con:
            row=con.execute("SELECT * FROM profiles WHERE user_id=?",(user_id,)).fetchone()
            return safe_profile(row) if row else {"role":"Data Analyst","location":"India","country":"in","skills":[],"saved_companies":[],"search_history":[]}
    def session_cookie(self,token,clear=False):
        secure=SESSION_COOKIE_SECURE or (self.headers.get("X-Forwarded-Proto","").lower()=="https") or bool(getattr(self,"is_secure",False))
        value="" if clear else token
        attrs=f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={0 if clear else SESSION_DAYS*86400}"
        return attrs+("; Secure" if secure else "")
    def create_session(self,user_id):
        token=secrets.token_urlsafe(32); token_hash=hashlib.sha256(token.encode()).hexdigest(); now=int(time.time())
        with account_db() as con:
            con.execute("DELETE FROM sessions WHERE expires_at<?",(now,))
            con.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",(token_hash,user_id,now+SESSION_DAYS*86400))
        return token
    def register_account(self,payload):
        if self.auth_rate_limited(): return self.send_json({"error":"Too many account attempts. Wait 15 minutes and try again."},status=429)
        email=str(payload.get("email","")).strip().lower(); password=str(payload.get("password",""))
        if len(email)>254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): return self.send_json({"error":"Enter a valid email address."},status=400)
        if len(password)<10 or len(password)>1024: return self.send_json({"error":"Use a password between 10 and 1024 characters."},status=400)
        salt=secrets.token_bytes(16); digest=password_digest(password,salt); now=datetime.now(timezone.utc).isoformat()
        try:
            with account_db() as con:
                cur=con.execute("INSERT INTO users(email,salt,password_hash,created_at) VALUES(?,?,?,?)",(email,salt,digest,now)); user_id=cur.lastrowid
                con.execute("INSERT INTO profiles(user_id,updated_at) VALUES(?,?)",(user_id,now))
        except sqlite3.IntegrityError: return self.send_json({"error":"An account with that email already exists. Sign in instead."},status=409)
        token=self.create_session(user_id)
        return self.send_json({"authenticated":True,"email":email,"profile":self.get_profile(user_id)},cookie=self.session_cookie(token))
    def login_account(self,payload):
        if self.auth_rate_limited(): return self.send_json({"error":"Too many account attempts. Wait 15 minutes and try again."},status=429)
        email=str(payload.get("email","")).strip().lower(); password=str(payload.get("password",""))
        with account_db() as con: user=con.execute("SELECT id,email,salt,password_hash FROM users WHERE email=?",(email,)).fetchone()
        if not user or not hmac.compare_digest(password_digest(password,user["salt"]),user["password_hash"]): return self.send_json({"error":"Email or password is incorrect."},status=401)
        token=self.create_session(user["id"])
        return self.send_json({"authenticated":True,"email":user["email"],"profile":self.get_profile(user["id"])},cookie=self.session_cookie(token))
    def auth_rate_limited(self):
        address=self.client_address[0] if self.client_address else "unknown"
        now=time.time()
        with AUTH_RATE_LOCK:
            recent=[timestamp for timestamp in AUTH_RATE_ATTEMPTS.get(address,[]) if now-timestamp<AUTH_RATE_WINDOW]
            if len(recent)>=AUTH_RATE_MAX:
                AUTH_RATE_ATTEMPTS[address]=recent
                return True
            recent.append(now)
            AUTH_RATE_ATTEMPTS[address]=recent
            if len(AUTH_RATE_ATTEMPTS)>2048:
                for ip,attempts in list(AUTH_RATE_ATTEMPTS.items()):
                    alive=[stamp for stamp in attempts if now-stamp<AUTH_RATE_WINDOW]
                    if alive: AUTH_RATE_ATTEMPTS[ip]=alive
                    else: AUTH_RATE_ATTEMPTS.pop(ip,None)
            return False
    def logout_account(self):
        cookies=http.cookies.SimpleCookie()
        try: cookies.load(self.headers.get("Cookie",""))
        except http.cookies.CookieError: pass
        morsel=cookies.get(SESSION_COOKIE)
        if morsel:
            with account_db() as con: con.execute("DELETE FROM sessions WHERE token_hash=?",(hashlib.sha256(morsel.value.encode()).hexdigest(),))
        return self.send_json({"authenticated":False},cookie=self.session_cookie("",True))
    def save_profile(self,payload):
        user=self.current_user()
        if not user: return self.send_json({"error":"Sign in to save your profile."},status=401)
        role=str(payload.get("role","Data Analyst")).strip()[:120] or "Data Analyst"; location=str(payload.get("location","India")).strip()[:120] or "India"; country=str(payload.get("country","in"))[:2].lower()
        if country not in {"in","us","gb","ca","au","de","fr","nl","sg","za"}: country="in"
        skills=payload.get("skills",[]); skills=skills if isinstance(skills,list) else str(skills).split(",")
        skills=list(dict.fromkeys(str(s).strip()[:60] for s in skills if str(s).strip()))[:40]
        known={x["name"] for x in DIRECTORY_COMPANIES if x["vendor"] in LIVE_VENDORS}
        chosen=payload.get("saved_companies",[]); chosen=chosen if isinstance(chosen,list) else []
        chosen=list(dict.fromkeys(str(x) for x in chosen if str(x) in known))[:3]
        now=datetime.now(timezone.utc).isoformat()
        with account_db() as con:
            old=con.execute("SELECT search_history_json FROM profiles WHERE user_id=?",(user["id"],)).fetchone()
            history=json.loads(old["search_history_json"]) if old else []
            search={"role":role,"location":location,"country":country,"searched_at":now}
            if not history or any(history[-1].get(k)!=search.get(k) for k in ("role","location","country")): history.append(search)
            history=history[-25:]
            con.execute("INSERT INTO profiles(user_id,role,location,country,skills_json,saved_companies_json,search_history_json,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,location=excluded.location,country=excluded.country,skills_json=excluded.skills_json,saved_companies_json=excluded.saved_companies_json,search_history_json=excluded.search_history_json,updated_at=excluded.updated_at",(user["id"],role,location,country,json.dumps(skills),json.dumps(chosen),json.dumps(history),now))
        return self.send_json({"saved":True,"profile":self.get_profile(user["id"])})
    def send_json(self,obj,status=None,cookie=None):
        data=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status or (200 if "error" not in obj else 502)); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.send_header("Referrer-Policy","strict-origin-when-cross-origin");
        if cookie: self.send_header("Set-Cookie",cookie)
        self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self,*args): pass

def mcp_stdio():
    """Minimal stdio MCP JSON-RPC server; read-only job search and market tools."""
    def reply(id_, result=None, error=None):
        out={"jsonrpc":"2.0","id":id_}
        out["error"]=error if error else {"code":0,"message":"","data":result}
        # MCP expects result envelope, not an error
        if not error: out={"jsonrpc":"2.0","id":id_,"result":result}
        sys.stdout.write(json.dumps(out,separators=(",",":"))+"\n"); sys.stdout.flush()
    while line:=sys.stdin.readline():
        try:
            msg=json.loads(line); method=msg.get("method"); id_=msg.get("id")
            if id_ is None: continue
            if method=="initialize": reply(id_,{"protocolVersion":msg.get("params",{}).get("protocolVersion","2025-03-26"),"capabilities":{"tools":{}},"serverInfo":{"name":"career-signal","version":"1.0.0"}})
            elif method=="notifications/initialized": continue
            elif method=="ping": reply(id_,{})
            elif method=="tools/list": reply(id_,{"tools":[{"name":"search_jobs","description":"Search live public listings across 200 employer career boards for a target role and location.","inputSchema":{"type":"object","properties":{"role":{"type":"string"},"location":{"type":"string"},"country":{"type":"string","description":"Country code such as in, gb or us"}},"required":["role"]}},{"name":"analyze_role_market","description":"Analyze current listings from 200 employer career boards and produce a skill-gap learning roadmap.","inputSchema":{"type":"object","properties":{"role":{"type":"string"},"location":{"type":"string"},"country":{"type":"string"},"current_skills":{"type":"array","items":{"type":"string"}},"weeks":{"type":"integer"}},"required":["role"]}},{"name":"company_role_requirements","description":"Show the skills explicitly required, preferred, or otherwise mentioned in each live-connected company's matching job descriptions, with short source excerpts. MNC career-site-only entries are marked as not live-indexed.","inputSchema":{"type":"object","properties":{"role":{"type":"string"},"location":{"type":"string"},"country":{"type":"string"},"company":{"type":"string","description":"Optional company name; omit for all companies"}},"required":["role"]}},{"name":"list_employer_sources","description":"List live job-board feeds plus major multinational official career sites. Site-only companies are marked as not live-integrated.","inputSchema":{"type":"object","properties":{}}}]})
            elif method=="tools/call":
                p=msg.get("params",{}); a=p.get("arguments",{}); name=p.get("name")
                if name=="search_jobs":
                    val=search_jobs(a.get("role","Data Analyst"),a.get("location","India"),a.get("country","in"))
                    val.pop("_analysis_jobs",None)
                    val["jobs"]=[{k:v for k,v in job.items() if k!="description"} for job in val["jobs"]]
                elif name=="analyze_role_market":
                    val=analyze_market(a.get("role","Data Analyst"),a.get("location","India"),a.get("country","in"),a.get("current_skills",[]),int(a.get("weeks",6)))
                    val.pop("company_pages",None)
                elif name=="company_role_requirements":
                    market=analyze_market(a.get("role","Data Analyst"),a.get("location","India"),a.get("country","in"),[],6)
                    pages=market.get("company_pages",[])
                    company=str(a.get("company","")).strip().casefold()
                    if company: pages=[p for p in pages if company in p["name"].casefold()]
                    val={"role":market.get("role"),"location":market.get("location"),"total_matching_jobs":market.get("total"),"boards_checked":market.get("boards_checked"),"companies":pages}
                elif name=="list_employer_sources": val={"count":len(DIRECTORY_COMPANIES),"live_board_count":len(LIVE_COMPANIES),"verified_at":SOURCE_REGISTRY.get("verified_at"),"companies":[{"name":s["name"],"provider":"Official career site" if s["vendor"]=="external" else s["vendor"].title(),"live_supported":s["vendor"] in LIVE_VENDORS,"sector":s.get("sector",""),"careers_url":s["careers_url"]} for s in DIRECTORY_COMPANIES]}
                else: raise ValueError("Unknown tool")
                reply(id_,{"content":[{"type":"text","text":json.dumps(val)}],"structuredContent":val,"isError":False})
            else: reply(id_,error={"code":-32601,"message":"Method not found"})
        except Exception as e:
            if "id_" in locals() and id_ is not None: reply(id_,error={"code":-32603,"message":str(e)})

if __name__=="__main__":
    if "--mcp" in sys.argv: mcp_stdio()
    else:
        DATA_DIR.mkdir(parents=True,exist_ok=True)
        init_account_db()
        port=int(os.getenv("PORT","8000")); print(f"Career Signal running at http://localhost:{port}")
        ThreadingHTTPServer((os.getenv("HOST","0.0.0.0"),port),Handler).serve_forever()
