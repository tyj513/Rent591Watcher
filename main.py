from bs4 import BeautifulSoup
import requests
from datetime import timedelta
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


def load_sent_ids(path=SENT_IDS_FILE):
    """讀取已處理的物件 ID，順便清掉過期的紀錄。回傳 {id: timestamp}。"""
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

    cutoff = time.time() - SEEN_TTL_DAYS * 86400
    pruned = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v >= cutoff}
    if len(pruned) != len(data):
        print(f'pruned {len(data) - len(pruned)} expired record(s)')
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

    def get_house_id(self):

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
        house_ids = []
        page = 1
        while page <= self.wanted_page:
            url = self.search_url if page < 2 else f'{self.search_url}&page={page}'
            r = self._fetch(s, url, headers, f'清單第 {page} 頁')
            if r is None or r.status_code != 200:
                print(f'清單第 {page} 頁重試後仍失敗，跳過')
                page += 1
                continue
            soup = BeautifulSoup(r.text, 'html.parser')
            house_ids += [i.get('href').split('/')[-1] for i in soup.find_all(class_="link v-middle")]
            page += 1
            time.sleep(random.uniform(1, 3))

        print(f'get {len(house_ids)} ids')
        return house_ids

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

    def generate_message(self, id, house_detail):
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
            f"\n🏠 {title}"
            f"\n{house_type} | {price} 元/月"
            f"\n坪數 {area} | {floor} | {shape}"
            f"\n{address}"
            f"\n{subsidy_line}"
            f"\n{time_}"
            f"\n{link}"
            )

    def transform_post_time(self, post_time):
        post_time = post_time.replace('此房屋在', '').replace('前發佈', '')
        if '秒鐘' in post_time:
            seconds_ago = int(re.search(r'(\d+)秒鐘', post_time).group(1))
            return timedelta(seconds=seconds_ago)
        elif '分鐘' in post_time:
            minutes_ago = int(re.search(r'(\d+)分鐘', post_time).group(1))
            return timedelta(minutes=minutes_ago)
        elif '小時' in post_time:
            hours_ago = int(re.search(r'(\d+)小時', post_time).group(1))
            return timedelta(hours=hours_ago)
        elif '天' in post_time:
            days_ago = int(re.search(r'(\d+)天', post_time).group(1))
            return timedelta(days=days_ago)
        else:
            return timedelta(0)

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
        house_ids = self.get_house_id()

        # 抓到 0 筆幾乎都代表出問題了（被擋、網址錯、591 改版），
        # 這種情況要讓 workflow 亮紅燈，不然會一直綠燈但其實什麼都沒做。
        if not house_ids:
            raise ScrapeEmptyError(
                '清單頁抓到 0 筆物件。可能原因：URL secret 內容有誤、591 擋掉了執行環境的 IP、'
                '或 591 改版導致選擇器 "link v-middle" 失效。'
                )

        # 已處理過的直接跳過，連詳情都不用抓（省下每筆 1~3 秒的等待）
        new_ids, skipped = [], 0
        for id in house_ids:
            if id in sent_ids:
                skipped += 1
            elif id not in new_ids:
                new_ids.append(id)
        print(f'{skipped} already processed, {len(new_ids)} to check')

        messages = []
        to_mark = []      # 這輪要記錄的 ID（沒送出的，例如太舊或解析失敗）
        pending = []      # 有訊息要送的 ID，等送成功才記錄
        consecutive_fails = 0
        for id in new_ids:
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

            if post_time_value is None:
                print(f"Warning: 'postTime' missing for house ID {id} or 'publish' data was invalid. Skipping time check.")
                continue

            post_time = self.transform_post_time(post_time_value)
            if post_time < timedelta(hours=self.within_hours):  # 只送 within_hours 小時內張貼的
                try:
                    messages.append(self.generate_message(id, house_detail))
                    pending.append(id)
                except (AttributeError, TypeError, IndexError) as e:
                    print(f'Warning: failed to build message for house ID {id}: {e}')
                    to_mark.append(id)
            else:
                to_mark.append(id)   # 太舊，記錄起來下次不用再抓
            time.sleep(0.1)

        print(f'{len(messages)} new house(s) to send')
        now = time.time()
        for id in to_mark:
            sent_ids[id] = now

        if self.send_line_messages(messages):
            for id in pending:
                sent_ids[id] = now
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
