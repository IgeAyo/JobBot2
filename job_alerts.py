import os, json, re, base64, requests, time
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
    cleaned = re.sub(r'[₦,\s]', '', str(text).lower())
    nums = re.findall(r'(\d+)(?:k|kngn|ngn)?', cleaned)
    if not nums: return 0
    values = [float(n) * (1000 if 'k' in cleaned else 1) for n in nums]
    return min(values)

def is_entry_level(title: str, desc: str) -> bool:
    combined = (title + " " + desc).lower()
    return any(tag in combined for tag in ENTRY_LEVEL_TAGS)

def filter_job(job: dict, debug: bool = False) -> bool:
    title = job.get("title", "")
    company = job.get("company", "")
    location = (job.get("location", "") or "").lower()
    salary_text = job.get("salary", "")
    description = job.get("description", "")
    
    if TARGET_LOCATION not in location and "lagos state" not in location:
        if debug: print(f"  ❌ Location fail: '{location}'")
        return False
    salary_num = parse_salary(salary_text)
    if salary_num < MIN_SALARY_NGN and salary_num > 0:
        if debug: print(f"  ❌ Salary fail: '{salary_text}' → ₦{salary_num:,.0f} < ₦{MIN_SALARY_NGN:,}")
        return False
    if not is_entry_level(title, description):
        if debug: print(f"  ❌ Level fail")
        return False
    title_desc = (title + " " + description).lower()
    if not any(kw in title_desc for kw in KEYWORDS):
        if debug: print(f"  ❌ Keyword fail")
        return False
    if debug: print(f"  ✅ MATCH: {title} @ {company}")
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
    
    # ✅ FIXED: Playwright-native pageFunction (no jQuery $)
    run_input = {
        "startUrls": [{"url": u} for u in urls],
        "maxCrawlPages": 20,
        "maxRequestRetries": 1,
        "requestTimeoutSecs": 45,
        "usePlaywright": True,
        "pageFunction": """async function pageFunction(context) {
            const { page, request } = context;
            // Wait for page to load
            await page.waitForLoadState('networkidle').catch(() => {});
            
            // Skip if not a job page
            const title = await page.title();
            if (!title.toLowerCase().includes('job') && !title.toLowerCase().includes('career')) return;
            
            // Extract using Playwright-native selectors (no $)
            const extractText = async (selector) => {
                try {
                    const el = await page.$(selector);
                    return el ? await el.evaluate(el => el.textContent.trim()) : '';
                } catch { return ''; }
            };
            
            return {
                title: await extractText('h1') || await extractText('[itemprop="title"]') || await extractText('.job-title') || title,
                company: await extractText('[itemprop="hiringOrganization"]') || await extractText('.company-name') || await extractText('.employer'),
                location: await extractText('[itemprop="jobLocation"]') || await extractText('.location') || await extractText('.job-location'),
                salary: await extractText('.salary') || await extractText('[itemprop="baseSalary"]') || await extractText('.remuneration'),
                url: request.url,
                description: await page.evaluate(() => document.body.innerText.slice(0, 1500))
            };
        }"""
    }
    
    print("🔄 Running Apify website crawler...")
    try:
        run = client.actor("apify/website-content-crawler").call(run_input=run_input, wait_secs=120)
        dataset = client.dataset(run["defaultDatasetId"]).list_items().items
        
        print(f"📦 Received {len(dataset)} raw items from crawler")
        
        # Debug: print first 3 items
        for i, item in enumerate(dataset[:3]):
            print(f"\n🔍 DEBUG ITEM {i+1}:")
            for key in ["title", "company", "location", "salary"]:
                val = item.get(key, "N/A")
                print(f"   {key}: {str(val)[:100]}")
        
        for item in dataset:
            url = item.get("url", "")
            if not url or url in seen_urls: continue
            seen_urls.add(url)
            job = {
                "title": item.get("title", "") or item.get("pageTitle", ""),
                "company": item.get("company", "") or item.get("organization", ""),
                "location": item.get("location", "") or item.get("address", ""),
                "salary": item.get("salary", ""),
                "url": url,
                "description": item.get("description", "") or item.get("text", "")
            }
            if filter_job(job, debug=True):
                matched.append(job)
                
    except Exception as e:
        print(f"⚠️ Apify crawl failed: {e}")
        import traceback; traceback.print_exc()
        return
    
    print(f"\n✅ Final: Found {len(matched)} matching jobs out of {len(seen_urls)} unique URLs crawled.")
    if matched:
        sync_to_sheets(matched)
        send_webhook(matched)
    else:
        print("📭 No new matches this run. Check debug logs above.")

if __name__ == "__main__":
    run()
