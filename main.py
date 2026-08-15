from bs4 import BeautifulSoup
import requests
from datetime import timedelta
import random
import json
import time
import re
import os
import sys

LINE_API_BASE = 'https://api.line.me/v2/bot/message'
LINE_TEXT_LIMIT = 4900   # LINE 單則文字上限 5000，留一點餘裕
LINE_MAX_MESSAGES = 5    # 單次 request 最多 5 則 message

SENT_IDS_FILE = 'sent_ids.json'   # 已處理過的物件 ID 紀錄
SEEN_TTL_DAYS = 14                # 紀錄保留天數，超過就清掉避免檔案無限長大


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
    def __init__(self, url: str, line_token: str, line_to: str = '', wanted_page: int = 2, within_hours: float = 8):
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/84.0.4147.125 Safari/537.36'
            }
        self.search_url = f"{url.replace('sort=posttime_desc', '')}&sort=posttime_desc"
        self.__line_token = line_token
        self.__line_to = line_to
        self.wanted_page = wanted_page
        self.within_hours = within_hours

    def get_house_id(self):

        # get token
        s = requests.Session()
        url = 'https://rent.591.com.tw/'
        r = s.get(url, headers=self.headers)
        soup = BeautifulSoup(r.text, 'html.parser')
        token = soup.find('meta', attrs={'name': 'csrf-token'})
        headers = self.headers.copy()
        if token and token.get('content'):
            headers['X-CSRF-TOKEN'] = token.get('content')

        # search
        house_ids = []
        page = 1
        while page <= self.wanted_page:
            url = self.search_url if page < 2 else f'{self.search_url}&page={page}'
            r = s.get(url, headers=headers)
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
        r = s.get(url, headers=headers)
        soup = BeautifulSoup(r.text, 'html.parser')
        token = soup.find('meta', attrs={'name': 'csrf-token'})

        if token and token.get('content'):
            headers['X-CSRF-TOKEN'] = token.get('content')
        device_id = s.cookies.get_dict().get('T591_TOKEN')
        if device_id:
            headers['deviceid'] = device_id
        headers['device'] = 'pc'

        url = f'https://bff.591.com.tw/v1/house/rent/detail?id={house_id}'
        r = s.get(url, headers=headers)
        house_detail = r.json().get('data')
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
        note = house_detail.get('favData').get('other').get('desc')
        link = f'https://rent.591.com.tw/{id}'

        return (f"\n{house_type} | {price} \n{area} | {floor} | {shape}\n{address}\n{time_}\n*{note}\n{link}")

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
        for id in new_ids:
            house_detail = self.get_house_detail(id)

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


if __name__ == '__main__':
    url = get_env('URL')                                  # 591 搜尋網址
    line_token = get_env('LINE_CHANNEL_ACCESS_TOKEN')     # LINE Messaging API channel access token
    line_to = get_env('LINE_TO', required=False)          # 選填：user/group id，沒填就用 broadcast
    wanted_page = int(os.environ.get('WANTED_PAGE', '').strip() or 2)
    within_hours = float(os.environ.get('WITHIN_HOURS', '').strip() or 8)   # 只通知幾小時內張貼的物件

    print(f'settings: wanted_page={wanted_page}, within_hours={within_hours}')
    sent_ids = load_sent_ids()
    print(f'loaded {len(sent_ids)} known id(s)')

    bot = Rent591Watcher(url, line_token, line_to, wanted_page, within_hours)
    sent_ids = bot.send_new_houses(sent_ids)

    save_sent_ids(sent_ids)
