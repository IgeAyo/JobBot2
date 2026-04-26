import os, json, re, base64, requests
from bs4 import BeautifulSoup
from apify_client import ApifyClient
import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")
SHEET_CREDS_B64 = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

KEYWORDS = ["python developer", "python intern", "it officer"]
TARGET_LOCATION = "lagos"
MIN_SALARY_NGN = 175000
ENTRY_LEVEL_TAGS = ["entry", "junior", "intern", "graduate", "associate", "nysc", "trainee", "0-1 year", "0-2 year"]

# ================= HELPER FUNCTIONS =================
def parse_salary(text: str) -> float:
    if not text: return 0
    has_k = "k" in str(text).lower()
    cleaned = re.sub(r'[₦,\s]', '', str(text).lower())
    nums = re.findall(r'(\d+(?:\.\d+)?)', cleaned)
    if not nums: return 0
    values = [float(n) * (1000 if has_k else 1) for n in nums]
    return min(values)

def is_entry_level(title: str, desc: str) -> bool:
    combined = (title + " " + desc).lower()
    return any(tag in combined for tag in ENTRY_LEVEL_TAGS)

def extract_job_from_html(html: str, url: str) -> dict:
    """Parse Jobberman/MyJobMag HTML with BeautifulSoup"""
    soup = BeautifulSoup(html, 'html.parser')
    
    # Try multiple selector strategies for Nigerian sites
    title = (
        soup.find('h1') or 
        soup.find('[itemprop="title"]') or 
        soup.find(class_=re.compile(r'job-title|title|position', re.I)) or
        soup.find('meta', property='og:title')
    )
    title_text = title.get('content', '').strip() if title and title.get('content') else (title.get_text(strip=True) if title else '')
    
    company = (
        soup.find('[itemprop="hiringOrganization"]') or
        soup.find(class_=re.compile(r'company-name|employer|organization', re.I)) or
        soup.find('a', class_=re.compile(r'company', re.I))
    )
    company_text = company.get_text(strip=True) if company else ''
    
    location = (
        soup.find('[itemprop="jobLocation"]') or
        soup.find(class_=re.compile(r'location|address|place', re.I))
    )
    location_text = location.get_text(strip=True) if location else ''
    
    salary = (
        soup.find(class_=re.compile(r'salary|remuneration|pay|wage', re.I)) or
        soup.find(string=re.compile(r'₦|salary|pay|ngn|k\/month', re.I))
    )
    salary_text = salary.get_text(strip=True) if salary and hasattr(salary, 'get_text') else (str(salary) if salary else '')
    
    # Fallback: search entire page text for keywords if selectors fail
    if not title_text or not location_text:
        page_text = soup.get_text(' ', strip=True)
        if not title_text:
            # Try to extract title from URL or meta
            title_text = url.split('/')[-1].replace('-', ' ').title() or soup.title.string if soup.title else ''
        if not location_text and TARGET_LOCATION in page_text.lower():
            location_text = TARGET_LOCATION
    
    return {
        "title": title_text[:200],
        "company": company_text[:100],
        "location": location_text[:100],
        "salary": salary_text[:100] if salary_text else '',
        "url": url,
        "description": page_text[:1500] if 'page_text' in locals() else soup.get_text(' ', strip=True)[:1500]
    }

def filter_job(job: dict, debug: bool = False, debug_id: str = "") -> bool:
    title = job.get("title", "") or ""
    company = job.get("company", "") or ""
    location = (job.get("location", "") or "").lower()
    salary_text = job.get("salary", "") or ""
    description = job.get("description", "") or ""
    
    if debug:
        print(f"\n🔎 FILTER DEBUG [{debug_id}]:")
        print(f"   title: '{title[:80]}{'...' if len(title)>80 else ''}'")
        print(f"   location: '{location}'")
        print(f"   salary: '{salary_text}'")
    
    # Location check (more flexible)
    if TARGET_LOCATION not in location and "lagos state" not in location and "vi" not in location and "ikeja" not in location and "surulere" not in location:
        if debug: print(f"   ❌ REJECTED: location '{location}' doesn't match Lagos areas")
        return False
    
    # Salary check (only reject if detected but too low)
    salary_num = parse_salary(salary_text)
    if salary_num > 0 and salary_num < MIN_SALARY_NGN:
        if debug: print(f"   ❌ REJECTED: salary ₦{salary_num:,.0f} < ₦{MIN_SALARY_NGN:,}")
        return False
    elif salary_num == 0 and debug:
        print(f"   ⚠️  WARNING: Could not parse salary from '{salary_text}'")
    
    # Entry-level check
    if not is_entry_level(title, description):
        if debug: print(f"   ❌ REJECTED: no entry-level tags")
        return False
    
    # Keyword check
    title_desc = (title + " " + description).lower()
    if not any(kw in title_desc for kw in KEYWORDS):
        if debug: print(f"   ❌ REJECTED: keywords not found")
        return False
    
    if debug: print(f"   ✅ PASSED ALL FILTERS")
    return True

def send_webhook(jobs: list):
    if not jobs: return
    payload = {"content": "🇳🇬 **New Job Matches Found**\n", "embeds": []}
    for j in jobs[:5]:
        payload["embeds"].append({
            "title": f"{j['title']} @ {j['company']}",
            "description": f"📍 {j['location']}\n💰 {j.get('salary', 'Negotiable')}\n🔗 [Apply Now]({j['url']})",
            "color": 5814783
        })
    requests.post(WEBHOOK_URL, json=payload)

def sync_to_sheets(jobs: list):
    if not jobs: return
    creds_data = base64.b64decode(SHEET_CREDS_B64)
    creds = Credentials.from_service_account_info(json.loads(creds_data), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).sheet1
    if sheet.row_count < 1:
        sheet.append_row(["Date Added", "Title", "Company", "Location", "Salary", "URL", "Status"])
    rows = [[os.getenv("GITHUB_SHA", "manual")[:7], j["title"], j["company"], j["location"], j.get("salary", ""), j["url"], "Not Applied"] for j in jobs]
    sheet.append_rows(rows, value_input_option="USER_ENTERED")

# ================= MAIN =================
def run():
    client = ApifyClient(APIFY_TOKEN)
    matched = []
    seen_urls = set()
    
    urls = []
    for kw in KEYWORDS:
        kw_enc = kw.replace(" ", "%20")
        urls.append(f"https://www.jobberman.com/jobs?keyword={kw_enc}&location=Lagos")
        urls.append(f"https://www.myjobmag.com/jobs/keyword/{kw.replace(' ', '-')}/lagos")
    
    # ✅ FIXED: Return raw HTML for Python parsing
    run_input = {
        "startUrls": [{"url": u} for u in urls],
        "maxCrawlPages": 20,
        "maxRequestRetries": 1,
        "requestTimeoutSecs": 45,
        "usePlaywright": True,
        # Simple pageFunction that just returns raw HTML
        "pageFunction": """async function pageFunction(context) {
            const { page, request } = context;
            await page.waitForLoadState('networkidle').catch(() => {});
            return {
                url: request.url,
                html: await page.content(),
                title: await page.title()
            };
        }"""
    }
    
    print("🔄 Running Apify crawler (raw HTML mode)...")
    try:
        run = client.actor("apify/website-content-crawler").call(run_input=run_input, wait_secs=120)
        dataset = client.dataset(run["defaultDatasetId"]).list_items().items
        print(f"📦 Received {len(dataset)} raw items from crawler")
        
        for idx, item in enumerate(dataset, 1):
            url = item.get("url", "")
            html = item.get("html", "")
            if not url or not html or url in seen_urls: continue
            seen_urls.add(url)
            
            # Parse HTML in Python
            job = extract_job_from_html(html, url)
            
            # Debug filter evaluation
            if filter_job(job, debug=True, debug_id=f"ITEM_{idx}"):
                matched.append(job)
                
    except Exception as e:
        print(f"⚠️ Crawl failed: {e}")
        import traceback; traceback.print_exc()
        return
    
    print(f"\n✅ Final: Found {len(matched)} matching jobs out of {len(seen_urls)} URLs.")
    if matched:
        sync_to_sheets(matched)
        send_webhook(matched)
    else:
        print("📭 No matches. Check FILTER DEBUG logs above.")

if __name__ == "__main__":
    run()
