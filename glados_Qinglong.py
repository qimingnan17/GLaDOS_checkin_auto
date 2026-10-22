import requests, json, os

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# pushplus秘钥
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
REQUEST_TIMEOUT = 15

GLADOS_CHECKIN_URL = "https://glados.rocks/api/user/checkin"
GLADOS_STATUS_URL = "https://glados.rocks/api/user/status"
GLADOS_REFERER = "https://glados.rocks/console/checkin"
GLADOS_ORIGIN = "https://glados.rocks"
GLADOS_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
GLADOS_PAYLOAD = {"token": "glados.one"}
PUSHPLUS_URL = "https://www.pushplus.plus/send"


def build_session():
    """创建带连接池和自动重试的 HTTP session。"""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def build_headers(cookie, json_body=False):
    headers = {
        'cookie': cookie,
        'referer': GLADOS_REFERER,
        'origin': GLADOS_ORIGIN,
        'user-agent': GLADOS_USER_AGENT,
    }
    if json_body:
        headers['content-type'] = 'application/json;charset=UTF-8'
    return headers


def send_pushplus(content, title='VPN签到成功'):
    """通过 PushPlus 推送消息；未配置 token 时静默跳过。"""
    if not PUSHPLUS_TOKEN or not content:
        return
    try:
        resp = requests.get(
            PUSHPLUS_URL,
            params={'token': PUSHPLUS_TOKEN, 'title': title, 'content': content},
            timeout=10,
        )
        if resp.status_code != 200:
            print('PushPlus推送失败: ' + resp.text)
    except Exception as e:
        print('PushPlus推送异常: ' + str(e))


def start():
    cookies = [c.strip() for c in os.environ.get("GLADOS_COOKIE", "").split("&") if c.strip()]
    if not cookies:
        print('未获取到COOKIE变量')
        return ''

    sendContent = ''
    session = build_session()
    for cookie in cookies:
        email = '未知账号'
        left_days = '?'
        try:
            checkin = session.post(
                GLADOS_CHECKIN_URL,
                headers=build_headers(cookie, json_body=True),
                data=json.dumps(GLADOS_PAYLOAD),
                timeout=REQUEST_TIMEOUT,
            )
            state = session.get(
                GLADOS_STATUS_URL,
                headers=build_headers(cookie),
                timeout=REQUEST_TIMEOUT,
            )

            try:
                state_json = state.json()
            except ValueError:
                state_json = {}
            state_data = state_json.get('data') if isinstance(state_json.get('data'), dict) else {}
            raw_left_days = state_data.get('leftDays')
            if raw_left_days is not None:
                left_days = str(raw_left_days).split('.')[0]
            email = state_data.get('email') or email

            try:
                checkin_json = checkin.json()
            except ValueError:
                checkin_json = None

            if checkin.ok and isinstance(checkin_json, dict) and 'message' in checkin_json:
                mess = checkin_json.get('message', '')
                print(email + '----' + mess + '----剩余(' + left_days + ')天')  # 日志输出
                sendContent += email + '----' + mess + '----剩余(' + left_days + ')天\n'
            else:
                send_pushplus(email + '更新cookie', 'GLaDOS Cookie失效')
                print(email + '----cookie已失效')
        except requests.RequestException as e:
            print(email + '----请求异常----' + str(e))
        except Exception as e:
            print(email + '----处理异常----' + str(e))

    send_pushplus(sendContent)
    return sendContent


def main_handler(event, context):
    return start()


if __name__ == '__main__':
    start()
