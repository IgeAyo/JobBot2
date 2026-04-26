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
    # Handle Nigerian formats: ₦175k, 175,000 NGN, ₦180,000 - ₦250,000
    cleaned = re.sub(r'[₦,\s]', '', str(text).lower())
    # Extract all numbers
    nums = re.findall(r'(\d+)(?:k|kngn|ngn)?', cleaned)
    if not nums: return 0
    # Convert 'k' suffix to thousands
    values = [float(n) * (1000 if 'k' in cleaned else 1) for n in nums]
    return min(values)  # Use lower bound of salary range

def is_entry_level(title: str, desc: str) -> bool:
    combined = (title + " " + desc).lower()
    return any(tag in combined for tag in ENTRY_LEVEL_TAGS)

def filter_job(job: dict, debug: bool = False) -> bool:
    title = job.get("title", "")
    company = job.get("company", "")
    location = (job.get("location", "") or "").lower()
    salary_text = job.get("salary", "")
    description = job.get("description", "")
    
    # Location check (more flexible)
    if TARGET_LOCATION not in location and "lagos state" not in location:
        if debug: print(f"  ❌ Location fail: '{location}'")
        return False
    
    # Salary check
    salary_num = parse_salary(salary_text)
    if salary_num < MIN_SALARY_NGN and salary_num > 0:  # Only filter if salary was detected
        if debug: print(f"  ❌ Salary fail: '{salary_text}' → ₦{salary_num:,.0f} < ₦{MIN_SALARY_NGN:,}")
        return False
    
    # Entry-level check
    if not is_entry_level(title, description):
        if debug: print(f"  ❌ Level fail: title='{title}', desc snippet='{description[:50]}...'")
        return False
    
    # Keyword check
    title_desc = (title + " " + description).lower()
    if not any(kw in title_desc for kw in KEYWORDS):
        if debug: print(f"  ❌ Keyword fail: none of {KEYWORDS} in title/desc")
        return False
    
    if debug: print(f"  ✅ MATCH: {title} @ {company} | ₦{salary_num:,.0f} | {location}")
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
    
    # Build search URLs for Nigerian job boards
    urls = []
    for kw in KEYWORDS:
        kw_enc = kw.replace(" ", "%20")
        urls.append(f"https://www.jobberman.com/jobs?keyword={kw_enc}&location=Lagos")
        urls.append(f"https://www.myjobmag.com/jobs/keyword/{kw.replace(' ', '-')}/lagos")
    
    run_input = {
        "startUrls": [{"url": u} for u in urls],
        "maxCrawlPages": 20,  # Lower for faster debug
        "maxRequestRetries": 1,
        "requestTimeoutSecs": 45,
        "pageFunction": """async function pageFunction(context) {
            const $ = context.jQuery;
            const pageTitle = $('title').text().toLowerCase();
            if (!pageTitle.includes('job') && !pageTitle.includes('career')) return;
            
            // Try multiple selectors for Nigerian sites
            return {
                title: $('h1').first().text().trim() || $('[itemprop="title"]').text().trim() || $('.job-title').text().trim(),
                company: $('[itemprop="hiringOrganization"]').text().trim() || $('.company-name').text().trim() || $('.employer').text().trim(),
                location: $('[itemprop="jobLocation"]').text().trim() || $('.location').text().trim() || $('.job-location').text().trim(),
                salary: $('.salary').text().trim() || $('[itemprop="baseSalary"]').text().trim() || $('.remuneration').text().trim(),
                url: context.request.url,
                description: $('body').text().slice(0, 1500)
            };
        }"""
    }
    
    print("🔄 Running Apify website crawler...")
    try:
        run = client.actor("apify/website-content-crawler").call(run_input=run_input, wait_secs=120)
        dataset = client.dataset(run["defaultDatasetId"]).list_items().items
        
        print(f"📦 Received {len(dataset)} raw items from crawler")
        
        # DEBUG: Print first 3 items to see what we got
        for i, item in enumerate(dataset[:3]):
            print(f"\n🔍 DEBUG ITEM {i+1}:")
            for key in ["title", "company", "location", "salary", "url"]:
                val = item.get(key, "N/A")
                print(f"   {key}: {val[:100] if isinstance(val, str) and len(val) > 100 else val}")
        
        for item in dataset:
            url = item.get("url", "")
            if not url or url in seen_urls: continue
            seen_urls.add(url)
            
            # Normalize keys from crawler output
            job = {
                "title": item.get("title", "") or item.get("pageTitle", ""),
                "company": item.get("company", "") or item.get("organization", ""),
                "location": item.get("location", "") or item.get("address", ""),
                "salary": item.get("salary", ""),
                "url": url,
                "description": item.get("description", "") or item.get("text", "")
            }
            
            # Debug filter evaluation
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
        print("📭 No new matches this run. Check debug logs above to tune filters.")

if __name__ == "__main__":
    run()
