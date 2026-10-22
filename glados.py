import requests, json, os, re, sys
from datetime import datetime
from zoneinfo import ZoneInfo

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 积分记录文件路径（同目录下），由 git checkout 恢复 + git commit 兜底
POINTS_LOG_FILE = "points_log.json"
# 每个账号保留最近 N 条签到明细，避免文件无限增长
MAX_HISTORY = 10
# 当前剩余积分初始值（首次记录或字段缺失时使用）
# 可通过工作流环境变量 DEFAULT_CURRENT_POINTS 覆盖
DEFAULT_CURRENT_POINTS = int(os.environ.get("DEFAULT_CURRENT_POINTS", "281"))
# 单次 HTTP 请求超时时间，避免 runner 被卡死
REQUEST_TIMEOUT = 15
# 业务日期固定使用北京时间，避免 UTC 日期导致跨天统计偏差
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")

GLADOS_CHECKIN_URL = "https://glados.rocks/api/user/checkin"
GLADOS_STATUS_URL = "https://glados.rocks/api/user/status"
GLADOS_REFERER = "https://glados.rocks/console/checkin"
GLADOS_ORIGIN = "https://glados.rocks"
GLADOS_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
GLADOS_PAYLOAD = {"token": "glados.one"}


def extract_points(message):
    """从 GLaDOS 签到返回的 message 中提取本次获得的积分。

    典型 message: "Checkin! Got 8 Points"
    重复签到或异常时通常无 "Got N Points" 字样，返回 0。
    """
    m = re.search(r'Got\s+(\d+)\s+Points', message or '')
    return int(m.group(1)) if m else 0


def load_points():
    """读取历史积分记录；文件不存在或解析失败时返回空字典。"""
    if not os.path.exists(POINTS_LOG_FILE):
        return {}
    try:
        with open(POINTS_LOG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        # 解析失败时给出明确信号，避免静默清空历史记录
        print(f"读取 {POINTS_LOG_FILE} 失败，将重新初始化: {e}")
        return {}


def save_points(data):
    """写入积分记录到本地文件。"""
    with open(POINTS_LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
    """按账号构造请求头。"""
    headers = {
        'cookie': cookie,
        'referer': GLADOS_REFERER,
        'origin': GLADOS_ORIGIN,
        'user-agent': GLADOS_USER_AGENT,
    }
    if json_body:
        headers['content-type'] = 'application/json;charset=UTF-8'
    return headers


def new_record():
    """返回一条空白的账号积分记录。"""
    return {
        'total_points': 0,
        'checkin_count': 0,
        'last_checkin_date': '',
        'last_points': 0,
        'left_days': '',
        'current_points': DEFAULT_CURRENT_POINTS,
        'recent': []
    }


# GLaDOS 签到 message 汉化映射表（小写匹配，避免大小写差异）
# 已知返回值：
#   "Checkin! Got N Points"                              — 签到成功获得 N 积分
#   "Checkin Repeats!"                                    — 当日重复签到
#   "Checkin Successfully!"                               — 签到成功（无积分字样）
#   "Today's observation logged. Return tomorrow..."     — 今日已签到，明天再来
# 未匹配到的 message 原样返回，便于后续补充
_MESSAGE_TRANSLATIONS = [
    (re.compile(r'got\s+(\d+)\s+points', re.IGNORECASE),
     lambda m: f'签到成功！获得 {m.group(1)} 积分'),
    (re.compile(r"today's\s+observation\s+logged", re.IGNORECASE),
     lambda m: '今日已签到，明天再来'),
    (re.compile(r'checkin\s+repeats', re.IGNORECASE),
     lambda m: '今日已签到，请勿重复'),
    (re.compile(r'checkin\s+successfully', re.IGNORECASE),
     lambda m: '签到成功'),
]


def translate_mess(mess):
    """将 GLaDOS 返回的英文 message 汉化为中文，未知 message 原样返回。"""
    if not mess:
        return mess
    for pat, fn in _MESSAGE_TRANSLATIONS:
        m = pat.search(mess)
        if m:
            return fn(m)
    return mess


# WxPusher 极简推送接口
WXPUSHER_URL = "https://wxpusher.zjiecode.com/api/send/message/simple-push"


def send_push(content, summary="", content_type=2):
    """通过 WxPusher 极简推送发送消息。

    使用环境变量 WXPUSHER_SPT 作为 simplePushToken，未配置时静默跳过。
    content_type: 1=文字 2=html(只发body内) 3=markdown
    """
    spt = os.environ.get("WXPUSHER_SPT", "")
    if not spt:
        return
    # summary 截断到 100 字以内，未提供时取 content 前 20 字
    if not summary:
        summary = content[:20]
    summary = summary[:100]
    payload = {
        "content": content,
        "summary": summary,
        "contentType": content_type,
        "spt": spt,
    }
    try:
        resp = requests.post(WXPUSHER_URL, json=payload, timeout=10)
        # WxPusher 成功返回 code=1000
        if resp.status_code != 200 or resp.json().get("code") != 1000:
            print("WxPusher推送失败: " + resp.text)
    except Exception as e:
        print("WxPusher推送异常: " + str(e))


# -------------------------------------------------------------------------------------------
# github workflows
# -------------------------------------------------------------------------------------------
if __name__ == '__main__':
    # WxPusher SPT（simplePushToken）申请地址 https://wxpusher.zjiecode.com
    # 由 send_push 函数内部读取环境变量 WXPUSHER_SPT 推送
    sendContent = ''
    # glados账号cookie 直接使用数组；环境变量用 & 分割，过滤空项
    cookies = [c.strip() for c in os.environ.get("GLADOS_COOKIE", "").split("&") if c.strip()]
    if not cookies:
        print('未获取到COOKIE变量')
        sys.exit(0)

    # 读取历史积分记录（由 git checkout 恢复，workflow 末尾 git commit 兜底）
    points_data = load_points()
    today = datetime.now(BUSINESS_TZ).strftime('%Y-%m-%d')
    session = build_session()

    try:
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
                    mess_raw = checkin_json.get('message', '')
                    # 汉化展示用文案，日志/推送/历史记录都用汉化版
                    mess = translate_mess(mess_raw)
                    record = points_data.get(email, new_record())
                    # 兼容旧记录：缺少 current_points 字段时初始化为默认值
                    if 'current_points' not in record:
                        record['current_points'] = DEFAULT_CURRENT_POINTS
                    # 从签到返回的 list 提取本次积分和当前余额，取不到则回退正则
                    checkin_list = checkin_json.get('list', [])
                    first_item = checkin_list[0] if isinstance(checkin_list, list) and checkin_list and isinstance(checkin_list[0], dict) else {}
                    if first_item:
                        try:
                            gained = int(float(first_item.get('change', 0)))
                        except (ValueError, TypeError):
                            gained = extract_points(mess_raw)
                        try:
                            record['current_points'] = int(float(first_item.get('balance', record['current_points'])))
                        except (ValueError, TypeError):
                            pass
                    else:
                        gained = extract_points(mess_raw)
                    # 防止同一天重复运行被多算（重复签到时 gained 通常已为 0，此处再加一层日期兜底）
                    if record.get('last_checkin_date') != today:
                        record['total_points'] = record.get('total_points', 0) + gained
                        record['checkin_count'] = record.get('checkin_count', 0) + 1
                    record['last_checkin_date'] = today
                    record['last_points'] = gained
                    record['left_days'] = left_days
                    recent = record.get('recent', [])
                    recent.insert(0, {'date': today, 'points': gained, 'left_days': left_days, 'message': mess})
                    if len(recent) > MAX_HISTORY:
                        recent = recent[:MAX_HISTORY]
                    record['recent'] = recent
                    points_data[email] = record

                    print(email+'----结果--'+mess+'----剩余('+left_days+')天----本次('+str(gained)+')----当前余额('+str(record['current_points'])+')')  # 日志输出
                    sendContent += email+'----'+mess+'----剩余('+left_days+')天----本次('+str(gained)+')----当前余额('+str(record['current_points'])+')\n'
                else:
                    send_push('<p>' + email + ' Cookie已失效，请尽快更新</p>', 'GLaDOS Cookie失效')
                    print(email + '----cookie已失效')  # 日志输出
            except requests.RequestException as e:
                # 单个账号网络失败不影响其他账号和最终持久化
                print(email + '----请求异常----' + str(e))
                sendContent += email + '----请求异常----' + str(e) + '\n'
            except Exception as e:
                # 单个账号数据异常不影响其他账号和最终持久化
                print(email + '----处理异常----' + str(e))
                sendContent += email + '----处理异常----' + str(e) + '\n'
    finally:
        # 无论中途是否异常，都先保存已成功处理的账号记录
        save_points(points_data)

    if sendContent:
        # 纯文本换行转 HTML <br>，加 H3 标题
        html_content = '<h3>GLaDOS 签到报告</h3>' + sendContent.replace('\n', '<br>')
        send_push(html_content, 'GLaDOS 签到报告')
