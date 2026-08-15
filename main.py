from bs4 import BeautifulSoup
import requests
from datetime import datetime, timedelta
import random
import json
import time
import re
import os
import sys
import html

LINE_API_BASE = 'https://api.line.me/v2/bot/message'
LINE_TEXT_LIMIT = 4900   # LINE 單則文字上限 5000，留一點餘裕
LINE_MAX_MESSAGES = 5    # 單次 request 最多 5 則 message

class ScrapeEmptyError(RuntimeError):
    """清單頁一筆都沒抓到，通常代表被擋或網址有問題。"""


# 591 沒有「可否租金補貼」的結構化欄位，只能從標題和屋況介紹的自由文字判斷。
SUBSIDY_KEYWORDS = ('租補', '租金補貼', '房屋補貼', '補貼', '補助')
# 出現在關鍵字前面就視為否定，例如「不可租補」「無法申請補貼」
SUBSIDY_NEGATIONS = ('不可', '不能', '無法', '恕不', '不提供', '不含', '沒有', '不予', '不接受', '不行')

# 591 會間歇性回 403（尤其從機房 IP），用退避重試把成功率拉高
RETRY_TRIES = int(os.environ.get('RETRY_TRIES', '').strip() or 4)
RETRY_BASE_WAIT = 8      # 第一次等 8 秒，之後 16、32…
RETRY_MAX_WAIT = 60
DETAIL_RETRY_TRIES = 2   # 單一物件抓不到不嚴重（下次會再抓），少試幾次避免整體太慢
MAX_CONSECUTIVE_FAILS = 6  # 連續這麼多筆抓不到就視為被擋，提早收工

SENT_IDS_FILE = 'sent_ids.json'   # 已處理過的物件 ID 紀錄
SEEN_TTL_DAYS = 14                # 紀錄保留天數，超過就清掉避免檔案無限長大

# 判斷「已通知過的物件是不是又被更新了」時的容許誤差。
# 清單頁的更新時間只到小時級（「3小時內更新」），換算回絕對時刻會有約 1 小時的誤差，
# 沒有這個緩衝就會因為進位跳動而重複通知同一筆。副作用是上次通知後 90 分鐘內的
# 再次更新會被吃掉，剛好也避免房東連續重刷造成洗頻。
UPDATE_TOLERANCE_SECONDS = 90 * 60

FINE, COARSE = 'fine', 'coarse'


def strip_html(raw):
    """屋況介紹是被跳脫過的 HTML（有時甚至跳脫兩次），還原成純文字。"""
    if not raw:
        return ''
    text = html.unescape(html.unescape(str(raw)))
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def detect_subsidy(house_detail):
    """判斷是否可申請租金補貼。回傳 '可申請' / '不可' / '未註明'。"""
    title = house_detail.get('title') or ''
    remark = house_detail.get('remark') or {}
    blob = f'{title} {strip_html(remark.get("content"))}'

    found_positive = False
    found_negative = False
    for kw in SUBSIDY_KEYWORDS:
        for m in re.finditer(re.escape(kw), blob):
            before = blob[max(0, m.start() - 6):m.start()]
            if any(neg in before for neg in SUBSIDY_NEGATIONS):
                found_negative = True
            else:
                found_positive = True

    if found_positive:
        return '可申請'
    if found_negative:
        return '不可'
    return '未註明'


def parse_age(text, now=None):
    """把 591 的時間字串換算成「距今多久」。回傳 (timedelta, 精度)，看不懂就回傳 (None, None)。

    591 的措辭（清單頁和詳情 API 共用同一套）：
      發佈：剛剛發佈 / N分鐘前發佈 / N小時前發佈 / N天前發佈 / 8月7日發佈
      更新：N分鐘內更新 / N小時內更新 / 昨日更新 / N天前更新

    精度 FINE 是秒／分／小時級，COARSE 是天級（昨日、N天前、絕對日期）。
    這個區分是給 _looks_updated 用的：小時級字串是上限值（「3小時內更新」），
    隨著時間過去換算出的絕對時刻只會往更早漂移，拿來比較很安全；天級字串在同一天內
    會往更晚漂移最多 24 小時，不能用來判斷是否比上次通知還新。
    """
    if not text:
        return None, None
    s = re.sub(r'\s+', '', str(text)).replace('此房屋在', '')

    if '剛剛' in s:
        return timedelta(0), FINE
    m = re.search(r'(\d+)秒', s)
    if m:
        return timedelta(seconds=int(m.group(1))), FINE
    m = re.search(r'(\d+)分鐘', s)
    if m:
        return timedelta(minutes=int(m.group(1))), FINE
    m = re.search(r'(\d+)小時', s)
    if m:
        return timedelta(hours=int(m.group(1))), FINE
    if '昨日' in s or '昨天' in s:
        return timedelta(days=1), COARSE
    m = re.search(r'(\d+)天', s)
    if m:
        return timedelta(days=int(m.group(1))), COARSE

    # 絕對日期：8月7日 / 2026-08-07 / 2026/8/7
    # 一定要排在「N個月」前面判斷，否則「8月7日」會被誤讀成 8 個月前。
    now = now or datetime.now()
    m = re.search(r'(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})', s)
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        try:
            dt = datetime(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None, None
        if not m.group(1) and dt > now:   # 沒寫年份又比今天晚，那是去年的
            dt = dt.replace(year=year - 1)
        return max(timedelta(0), now - dt), COARSE

    m = re.search(r'(\d+)個月', s)
    if m:
        return timedelta(days=30 * int(m.group(1))), COARSE

    return None, None


def extract_price(house_detail):
    """取出租金數字，取不到回傳 None。"""
    price = (house_detail.get('favData') or {}).get('price') if isinstance(house_detail, dict) else None
    if isinstance(price, bool):
        return None
    if isinstance(price, (int, float)):
        return float(price)
    if isinstance(price, str):   # 保險：有時候會是 "12,000" 這種字串
        digits = re.sub(r'[^\d.]', '', price)
        try:
            return float(digits) if digits else None
        except ValueError:
            return None
    return None


def make_record(ts, price=None, previous=None):
    """組出一筆紀錄 {'t': 時戳, 'p': 租金}。

    't' 是「上次評估這筆的時間」，判斷後續更新的基準。
    'p' 是當時的租金，用來判斷房東按的「更新」到底有沒有改內容。
    這次抓不到價格時沿用舊快照，不要把已知的價格洗掉。
    """
    record = {'t': float(ts)}
    if price is None and isinstance(previous, dict):
        price = previous.get('p')
    if price is not None:
        record['p'] = price
    return record


def load_sent_ids(path=SENT_IDS_FILE):
    """讀取已處理的物件 ID，順便清掉過期的紀錄。回傳 {id: {'t': 時戳, 'p': 租金}}。"""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f'{path} not found, starting fresh')
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f'Warning: cannot read {path} ({e}), starting fresh')
        return {}

    if not isinstance(data, dict):
        print(f'Warning: {path} has unexpected format, starting fresh')
        return {}

    # 舊格式是 {id: 時戳}，直接沿用不用手動搬移；沒有價格快照的那幾筆，
    # 第一次再抓到時會把價格補上，補上之前不會因為「沒東西可比」就亂送。
    normalized, legacy = {}, 0
    for id, value in data.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            normalized[id] = {'t': float(value)}
            legacy += 1
        elif isinstance(value, dict) and isinstance(value.get('t'), (int, float)):
            normalized[id] = make_record(value['t'], value.get('p'))
    if legacy:
        print(f'{legacy} 筆是舊格式（只有時戳），價格快照會在下次抓到時補上')

    cutoff = time.time() - SEEN_TTL_DAYS * 86400
    pruned = {k: v for k, v in normalized.items() if v['t'] >= cutoff}
    dropped = len(data) - len(pruned)
    if dropped:
        print(f'pruned {dropped} expired/invalid record(s)')
    return pruned


def save_sent_ids(sent_ids, path=SENT_IDS_FILE):
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(sent_ids, f, ensure_ascii=False, indent=0, sort_keys=True)
        print(f'saved {len(sent_ids)} id(s) to {path}')
    except OSError as e:
        print(f'Warning: cannot write {path}: {e}')


class Rent591Watcher:
    def __init__(self, url: str, line_token: str, line_to: str = '', wanted_page: int = 2, within_hours: float = 24):
        # 盡量貼近真實瀏覽器，降低被判定成機器人的機率。
        # 注意：不要寫死 Cookie（Session 會自動處理）也不要寫死 Host（requests 會依網址自動設定，
        # 寫死會讓 bff.591.com.tw 那支 API 掛掉）。Accept-Encoding 不放 br，避免沒裝 brotli 導致亂碼。
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-User': '?1',
            }
        self.search_url = f"{url.replace('sort=posttime_desc', '')}&sort=posttime_desc"
        self.__line_token = line_token
        self.__line_to = line_to
        self.wanted_page = wanted_page
        self.within_hours = within_hours
        self.send_failed = False

    def _fetch(self, session, url, headers, label, tries=None):
        """591 會間歇性回 403，退避後重試；每次重試前重新暖身取得新的 cookie。"""
        tries = tries or RETRY_TRIES
        r = None
        for attempt in range(1, tries + 1):
            try:
                r = session.get(url, headers=headers, timeout=30)
            except requests.RequestException as e:
                print(f'Warning: {label} 連線失敗 ({e.__class__.__name__}) [{attempt}/{tries}]')
                r = None
            else:
                if r.status_code == 200:
                    return r
                print(f'Warning: {label} returned HTTP {r.status_code} [{attempt}/{tries}]')

            if attempt == tries:
                break

            wait = min(RETRY_MAX_WAIT, RETRY_BASE_WAIT * 2 ** (attempt - 1)) + random.uniform(0, 5)
            print(f'  {wait:.0f} 秒後重試…')
            time.sleep(wait)
            try:   # 重新暖身，換一組 session cookie
                session.get('https://rent.591.com.tw/', headers=self.headers, timeout=30)
            except requests.RequestException:
                pass
        return r

    @staticmethod
    def _parse_list_page(html_text):
        """從清單頁抓出 [(物件 ID, 更新時間字串)]。

        清單頁沒有發佈時間，只有更新時間（在 div.item-info 裡的 span.line），
        剛好夠拿來初篩「這筆是不是又被更新了」，不用每筆都去抓詳情。
        """
        soup = BeautifulSoup(html_text, 'html.parser')
        items = []
        for card in soup.find_all('div', class_='item-info'):
            link = card.find(class_='link v-middle')
            href = link.get('href') if link else None
            if not href:
                continue
            # 有些 href 會帶 query（例如 ?is_ai_video=1），不切掉會污染 ID
            house_id = href.split('?')[0].split('#')[0].rstrip('/').split('/')[-1]
            if not house_id.isdigit():
                print(f'Warning: 無法從 href 取出物件 ID：{href}')
                continue
            update_label = next(
                (t for t in (sp.get_text(strip=True) for sp in card.find_all('span', class_='line'))
                 if '更新' in t),
                None,
                )
            items.append((house_id, update_label))
        return items

    def get_house_list(self):
        """回傳 [(物件 ID, 更新時間字串)]，已依清單頁順序去重。"""

        # get token
        s = requests.Session()
        url = 'https://rent.591.com.tw/'
        r = self._fetch(s, url, self.headers, '首頁')
        if r is None:
            return []
        soup = BeautifulSoup(r.text, 'html.parser')
        token = soup.find('meta', attrs={'name': 'csrf-token'})
        headers = self.headers.copy()
        if token and token.get('content'):
            headers['X-CSRF-TOKEN'] = token.get('content')

        # search
        headers['Referer'] = 'https://rent.591.com.tw/'
        items, seen = [], set()
        page = 1
        while page <= self.wanted_page:
            url = self.search_url if page < 2 else f'{self.search_url}&page={page}'
            r = self._fetch(s, url, headers, f'清單第 {page} 頁')
            if r is None or r.status_code != 200:
                print(f'清單第 {page} 頁重試後仍失敗，跳過')
                page += 1
                continue
            for house_id, update_label in self._parse_list_page(r.text):
                if house_id in seen:
                    continue
                seen.add(house_id)
                items.append((house_id, update_label))
            page += 1
            time.sleep(random.uniform(1, 3))

        missing = sum(1 for _, label in items if not label)
        print(f'get {len(items)} ids ({missing} 筆沒有更新時間標籤)')
        # 全部都抓不到標籤代表 591 改版了，更新偵測會整組失效，要讓 log 看得出來
        if items and missing == len(items):
            print('Warning: 清單頁一筆更新時間都沒抓到，591 可能改版了（原本在 div.item-info 的 span.line）。'
                  '「有更新就通知」這條規則會失效，只剩新上架會通知。')
        return items

    def get_house_detail(self, house_id):
        headers = self.headers.copy()

        s = requests.Session()
        url = f'https://rent.591.com.tw/{house_id}'
        r = self._fetch(s, url, headers, f'物件 {house_id} 頁面', tries=DETAIL_RETRY_TRIES)
        if r is None or r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        token = soup.find('meta', attrs={'name': 'csrf-token'})

        if token and token.get('content'):
            headers['X-CSRF-TOKEN'] = token.get('content')
        device_id = s.cookies.get_dict().get('T591_TOKEN')
        if device_id:
            headers['deviceid'] = device_id
        headers['device'] = 'pc'

        # 這支是 XHR API，headers 要換成 ajax 的樣子，Referer 指回該物件頁
        headers['Accept'] = 'application/json, text/plain, */*'
        headers['Referer'] = url
        headers['Sec-Fetch-Dest'] = 'empty'
        headers['Sec-Fetch-Mode'] = 'cors'
        headers['Sec-Fetch-Site'] = 'same-site'
        headers.pop('Upgrade-Insecure-Requests', None)
        headers.pop('Sec-Fetch-User', None)
        headers.pop('Cache-Control', None)

        url = f'https://bff.591.com.tw/v1/house/rent/detail?id={house_id}'
        r = self._fetch(s, url, headers, f'物件 {house_id} API', tries=DETAIL_RETRY_TRIES)
        if r is None or r.status_code != 200:
            return None
        try:
            house_detail = r.json().get('data')
        except ValueError:
            print(f'Warning: house {house_id} detail is not JSON (HTTP {r.status_code})')
            house_detail = None
        time.sleep(random.uniform(1, 3))
        print(f'get {house_id} detail')
        return house_detail

    def generate_message(self, id, house_detail, badge=''):
        house_type = house_detail.get('favData').get('kindTxt')
        price = f"{house_detail.get('favData').get('price'):,.0f}"
        area = f"{house_detail.get('favData').get('area')}坪"
        floor = house_detail.get('info')[2].get('value') if len(house_detail.get('info')) > 2 else None
        shape = house_detail.get('gtm_detail_data').get('shape_name')
        address = house_detail.get('favData').get('address').replace('台北市', '')
        post_time = house_detail.get('publish').get('postTime').replace('此房屋在', '')
        update_time = house_detail.get('publish').get('updateTime')
        time_ = f"{post_time}{' | ' + update_time if update_time else ''}"
        link = f'https://rent.591.com.tw/{id}'

        title = (house_detail.get('title') or '').strip()
        if len(title) > 40:
            title = title[:40] + '…'

        subsidy = detect_subsidy(house_detail)
        subsidy_line = {'可申請': '租補：可申請 ✅', '不可': '租補：不可 ❌'}.get(subsidy, '租補：未註明')

        return (
            f"\n{badge + ' ' if badge else ''}🏠 {title}"
            f"\n{house_type} | {price} 元/月"
            f"\n坪數 {area} | {floor} | {shape}"
            f"\n{address}"
            f"\n{subsidy_line}"
            f"\n{time_}"
            f"\n{link}"
            )

    def _looks_updated(self, update_label, last_seen_ts, now):
        """清單頁的更新時間看起來比上次通知還新嗎？（初篩用，只為了決定要不要花時間抓詳情）"""
        age, precision = parse_age(update_label)
        if age is None:
            return False
        if precision != FINE:
            # 天級字串（昨日更新 / N天前更新）在同一天內會往後漂移最多 24 小時，
            # 無法分辨「真的又更新了」和「只是字串自己在變」，所以一律當成沒更新。
            # 代價是隔天以上才被更新的物件不會補通知，但那也早就過了通知窗。
            return False
        if age >= timedelta(hours=self.within_hours):
            return False
        return (now - age.total_seconds()) > last_seen_ts + UPDATE_TOLERANCE_SECONDS

    def _chunk_texts(self, texts):
        """把多筆物件訊息合併成幾則不超過 LINE 上限的文字訊息。"""
        chunks, current = [], ''
        for text in texts:
            text = text.strip()
            if not text:
                continue
            if len(text) > LINE_TEXT_LIMIT:
                text = text[:LINE_TEXT_LIMIT]
            candidate = f'{current}\n\n{text}' if current else text
            if len(candidate) > LINE_TEXT_LIMIT:
                chunks.append(current)
                current = text
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    def send_line_messages(self, texts):
        """有設定 LINE_TO 就用 push，否則用 broadcast（發給所有加好友的人）。回傳是否全部成功。"""
        chunks = self._chunk_texts(texts)
        if not chunks:
            return True

        ok = True
        headers = {
            'Authorization': f'Bearer {self.__line_token}',
            'Content-Type': 'application/json',
            }
        endpoint = f'{LINE_API_BASE}/push' if self.__line_to else f'{LINE_API_BASE}/broadcast'

        for i in range(0, len(chunks), LINE_MAX_MESSAGES):
            batch = chunks[i:i + LINE_MAX_MESSAGES]
            payload = {'messages': [{'type': 'text', 'text': t} for t in batch]}
            if self.__line_to:
                payload['to'] = self.__line_to

            r = requests.post(endpoint, headers=headers, data=json.dumps(payload).encode('utf-8'))
            if r.status_code != 200:
                print(f'LINE send failed ({r.status_code}): {r.text}')
                ok = False
            else:
                print(f'LINE sent {len(batch)} message(s)')
            time.sleep(0.5)

        return ok

    def send_new_houses(self, sent_ids=None):
        sent_ids = {} if sent_ids is None else sent_ids
        listings = self.get_house_list()

        # 抓到 0 筆幾乎都代表出問題了（被擋、網址錯、591 改版），
        # 這種情況要讓 workflow 亮紅燈，不然會一直綠燈但其實什麼都沒做。
        if not listings:
            raise ScrapeEmptyError(
                '清單頁抓到 0 筆物件。可能原因：URL secret 內容有誤、591 擋掉了執行環境的 IP、'
                '或 591 改版導致選擇器 "link v-middle" 失效。'
                )

        now = time.time()
        window = timedelta(hours=self.within_hours)
        known_at_start = set(sent_ids)   # 用來分辨訊息該標「新上架」還是「有更新」

        # 初篩：清單頁只有更新時間沒有發佈時間，所以
        #   沒見過的 ID → 可能是新上架，抓詳情確認發佈時間
        #   見過的 ID   → 只有更新時間看起來比上次通知還新才抓詳情（省下每筆 1~3 秒的等待）
        candidates, skipped = [], 0
        for id, update_label in listings:
            if id not in sent_ids:
                candidates.append(id)
            elif self._looks_updated(update_label, sent_ids[id]['t'], now):
                print(f'{id} 被刷新過（{update_label}），抓詳情比對價格')
                candidates.append(id)
            else:
                skipped += 1
        print(f'{skipped} unchanged, {len(candidates)} to check')

        messages = []
        to_mark = {}      # {ID: 租金} 沒送出但要記錄的（太舊、解析失敗、或只是被刷新沒改內容）
        pending = {}      # {ID: 租金} 有訊息要送的，等送成功才記錄
        consecutive_fails = 0
        for id in candidates:
            house_detail = self.get_house_detail(id)

            # 連續抓不到通常代表整個被擋了，繼續硬跑只是浪費時間。
            # 這些 ID 不會被標記，下次執行會重新嘗試。
            if house_detail is None:
                consecutive_fails += 1
                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    print(f'連續 {consecutive_fails} 筆抓不到詳情，判定被擋，提早結束本輪')
                    break
            else:
                consecutive_fails = 0

            if isinstance(house_detail, str):
                try:
                    house_detail = json.loads(house_detail)
                except json.JSONDecodeError:
                    print(f"Error: Failed to decode full house details JSON string for house ID {id}. Skipping house.")
                    continue

            if not isinstance(house_detail, dict):
                print(f"Warning: no detail data for house ID {id}. Skipping house.")
                continue

            publish_data_raw = house_detail.get('publish')
            publish_data = None
            if isinstance(publish_data_raw, str):
                try:
                    publish_data = json.loads(publish_data_raw)
                except json.JSONDecodeError:
                    print(f"Error: Failed to decode 'publish' JSON string for house ID {id}. Skipping house.")
                    continue
            elif isinstance(publish_data_raw, dict):
                publish_data = publish_data_raw

            post_time_value = publish_data.get('postTime') if isinstance(publish_data, dict) else None
            update_time_value = publish_data.get('updateTime') if isinstance(publish_data, dict) else None

            if post_time_value is None and update_time_value is None:
                print(f"Warning: 'postTime' missing for house ID {id} or 'publish' data was invalid. Skipping time check.")
                continue

            post_age, _ = parse_age(post_time_value)
            update_age, _ = parse_age(update_time_value)
            ages = [a for a in (post_age, update_age) if a is not None]
            price = extract_price(house_detail)

            # 兩個時間都看不懂就不要送。寧可漏通知，也不要像以前一樣把解析失敗
            # 當成「剛剛發佈」而把幾個月前的物件全部推出去。
            if not ages:
                print(f'Warning: 物件 {id} 的時間字串無法解析（postTime={post_time_value!r}, '
                      f'updateTime={update_time_value!r}），本輪不送。591 可能改了措辭，請檢查 parse_age。')
                to_mark[id] = price
                continue

            if min(ages) >= window:
                to_mark[id] = price   # 太舊，記錄起來下次不用再抓
                time.sleep(0.1)
                continue

            badge = None
            if id not in known_at_start:
                # 沒見過的 ID：發佈時間在窗內就是新上架，否則是被刷新才浮上來的舊物件
                badge = '🆕 新上架' if post_age is not None and post_age < window else None
                if badge is None:
                    # 第一次看到但已經不新，先把價格快照存起來當基準，之後降價才通知
                    to_mark[id] = price
                    time.sleep(0.1)
                    continue
            else:
                # 見過的 ID：591 的「更新」多半只是房東刷曝光度，所以比對價格，
                # 只有租金真的變了才通知。沒變就只把基準往前推，不吵使用者。
                old_price = sent_ids.get(id, {}).get('p')
                if old_price is None or price is None:
                    print(f'{id} 沒有可比對的價格快照，這輪先補記錄不通知')
                elif price != old_price:
                    arrow = '🔻 降價' if price < old_price else '🔺 漲價'
                    badge = f'{arrow} {old_price:,.0f} → {price:,.0f}'
                    print(f'{id} 租金變動 {old_price:,.0f} -> {price:,.0f}')
                if badge is None:
                    to_mark[id] = price
                    time.sleep(0.1)
                    continue

            try:
                messages.append(self.generate_message(id, house_detail, badge))
                pending[id] = price
            except (AttributeError, TypeError, IndexError) as e:
                print(f'Warning: failed to build message for house ID {id}: {e}')
                to_mark[id] = price
            time.sleep(0.1)

        print(f'{len(messages)} new house(s) to send')
        # 時戳的意義是「上次評估這筆的時間」，也就是判斷後續更新的基準。
        # to_mark 裡的都是真的抓過詳情、確認過不用送的，所以基準往前推是對的：
        # 之後真的有新更新還是會比這個時間晚而被抓出來，而清單頁跟詳情偶爾對不上時
        # 也不會每輪都重抓同一筆。純粹被初篩跳過（沒抓詳情）的不會進來，基準保持不動。
        for id, price in to_mark.items():
            sent_ids[id] = make_record(now, price, sent_ids.get(id))

        if self.send_line_messages(messages):
            for id, price in pending.items():
                sent_ids[id] = make_record(now, price, sent_ids.get(id))
        else:
            self.send_failed = True
            print('Warning: some messages failed to send, those ids will be retried next run')

        return sent_ids


def get_env(name, required=True, default=''):
    value = os.environ.get(name, '').strip()
    if not value:
        if required:
            print(f'Error: environment variable {name} is missing or empty.')
            sys.exit(1)
        return default
    return value


def main():
    """回傳結束碼：0 正常，1 有問題（讓 GitHub Actions 亮紅燈）。"""
    url = get_env('URL')                                  # 591 搜尋網址
    line_token = get_env('LINE_CHANNEL_ACCESS_TOKEN')     # LINE Messaging API channel access token
    line_to = get_env('LINE_TO', required=False)          # 選填：user/group id，沒填就用 broadcast
    wanted_page = int(os.environ.get('WANTED_PAGE', '').strip() or 2)
    within_hours = float(os.environ.get('WITHIN_HOURS', '').strip() or 8)   # 只通知幾小時內張貼的物件

    print(f'settings: wanted_page={wanted_page}, within_hours={within_hours}')
    sent_ids = load_sent_ids()
    print(f'loaded {len(sent_ids)} known id(s)')

    bot = Rent591Watcher(url, line_token, line_to, wanted_page, within_hours)
    try:
        sent_ids = bot.send_new_houses(sent_ids)
    except ScrapeEmptyError as e:
        print(f'Error: {e}')
        return 1   # 這裡不存檔，避免把既有紀錄清空

    save_sent_ids(sent_ids)

    # 先存檔再回報失敗，成功處理的紀錄才不會丟失；沒送出的下次會自動重送
    if bot.send_failed:
        print('Error: LINE 發送失敗，請檢查 LINE_CHANNEL_ACCESS_TOKEN 是否正確。')
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
