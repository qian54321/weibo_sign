#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
微博超话批量签到 · 华为云 FunctionGraph 版 (移动端 API)
基于移动端 m.weibo.cn 接口，签到 URL 由服务端返回，无需手动拼参数

环境变量(在函数"配置 -> 环境变量"中设置):
  WEIBO_COOKIE    单账户 Cookie 整串(与 WEIBO_COOKIES 二选一, 优先级低)
  WEIBO_COOKIES   多账户, 多个 Cookie 用 @ 或换行或 ---- 分隔
  START_JITTER_MAX 启动随机抖动上限(秒), 默认 180; 设为 0 关闭
  DELAY_MIN       超话间随机延迟下限(秒), 默认 3
  DELAY_MAX       超话间随机延迟上限(秒), 默认 6
  ACCOUNT_INTERVAL 多账户之间基准间隔(秒), 默认 10
  WEIBO_TOPICS    可选; 只签指定超话, 多个用逗号分隔, 如: 侯明昊,夭玹
                  不设置则签全部
  WXPUSHER_APP_TOKEN WxPusher 的 appToken(AT_开头)
  WXPUSHER_UID    接收者的 UID(UID_开头), 多人用英文逗号分隔
  WXPUSHER_TOPIC  可选; WxPusher 主题ID(数字)
"""

import json
import os
import random
import re
import ssl
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ==================== 默认配置 ====================
DEFAULT_START_JITTER_MAX = 180
DEFAULT_DELAY_MIN = 3.0
DEFAULT_DELAY_MAX = 6.0
DEFAULT_ACCOUNT_INTERVAL = 10
HTTP_TIMEOUT = 15

BASE_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
    'MWeibo-Pwa': '1',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}

MOBILE_API_BASE = 'https://m.weibo.cn'


def log(message, level='INFO'):
    timestamp = time.strftime('%H:%M:%S', time.localtime())
    symbols = {'INFO': 'ℹ️', 'SUCCESS': '✅', 'ERROR': '❌', 'WARNING': '⚠️'}
    print(f"[{timestamp}] {symbols.get(level, 'ℹ️')} {message}")


# ==================== 环境变量读取 ====================
def get_env(context, key, default='', event=None):
    # 优先从 event 读取(测试事件)
    if event and key in event:
        return event[key]
    try:
        value = context.getUserData(key)
        if value:
            return value
    except Exception:
        pass
    return os.getenv(key, default)


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def build_config(context, event=None):
    return {
        'start_jitter_max': _to_float(get_env(context, 'START_JITTER_MAX',
                                               DEFAULT_START_JITTER_MAX, event=event),
                                      DEFAULT_START_JITTER_MAX),
        'delay_min': _to_float(get_env(context, 'DELAY_MIN', DEFAULT_DELAY_MIN, event=event),
                               DEFAULT_DELAY_MIN),
        'delay_max': _to_float(get_env(context, 'DELAY_MAX', DEFAULT_DELAY_MAX, event=event),
                               DEFAULT_DELAY_MAX),
        'account_interval': _to_float(get_env(context, 'ACCOUNT_INTERVAL',
                                              DEFAULT_ACCOUNT_INTERVAL, event=event),
                                      DEFAULT_ACCOUNT_INTERVAL),
        'wxpusher_token': get_env(context, 'WXPUSHER_APP_TOKEN', '', event=event),
        'wxpusher_uids': [u for u in re.split(r'[,，\s]+',
                          get_env(context, 'WXPUSHER_UID', '', event=event))
                          if u.strip()],
        'wxpusher_topic': get_env(context, 'WXPUSHER_TOPIC', '', event=event).strip(),
        'topics': [t.strip() for t in re.split(r'[,，]+',
                   get_env(context, 'WEIBO_TOPICS', '', event=event))
                   if t.strip()],
    }


# ==================== HTTP 工具 ====================
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def http_get(url, headers):
    req = Request(url, headers=headers)
    with urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
        status = resp.getcode()
        body = resp.read().decode('utf-8', errors='replace')
        return status, body


def http_post_json(url, payload, headers=None):
    data = json.dumps(payload).encode('utf-8')
    hdrs = {'Content-Type': 'application/json'}
    if headers:
        hdrs.update(headers)
    req = Request(url, data=data, headers=hdrs)
    with urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
        status = resp.getcode()
        body = resp.read().decode('utf-8', errors='replace')
        return status, body


# ==================== Cookie 管理 ====================
def parse_cookie_str(cookie_str):
    jar = {}
    for part in cookie_str.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        key, _, value = part.partition('=')
        if key.strip():
            jar[key.strip()] = value.strip()
    return jar


def cookie_header(jar):
    return '; '.join(f'{k}={v}' for k, v in jar.items())


def clean_cookie(cookie):
    try:
        cookie = cookie.strip().replace('\n', '').replace('\r', '')
        if isinstance(cookie, bytes):
            cookie = cookie.decode('utf-8', errors='ignore')
        cookie = ''.join(char for char in cookie if ord(char) < 128)
        return cookie
    except Exception as e:
        log(f"Cookie处理失败: {str(e)}", 'ERROR')
        return cookie


# ==================== 签到核心(移动端 API) ====================
class WeiboChaohuaSignin:
    def __init__(self, cookie, config, account_index=1, total_accounts=1):
        self.config = config
        self.account_index = account_index
        self.total_accounts = total_accounts
        self.account_name = f"账户{account_index}"
        self.screen_name = None
        self.uid = None

        self.cookie = clean_cookie(cookie)
        self.jar = parse_cookie_str(self.cookie)

    def get_user_info(self):
        if self.screen_name:
            return self.screen_name
        sub_match = re.search(r'SUB=([^;]+)', self.cookie)
        if sub_match:
            return f"用户{sub_match.group(1)[:8]}..."
        return "未知用户"

    def _headers(self, extra=None):
        headers = dict(BASE_HEADERS)
        headers['Cookie'] = cookie_header(self.jar)
        headers['Referer'] = 'https://m.weibo.cn/p/tabbar?containerid=100803_-_recentvisit&page_type=tabbar'
        if extra:
            headers.update(extra)
        return headers

    def _sleep_random(self, low=None, high=None):
        low = self.config['delay_min'] if low is None else low
        high = self.config['delay_max'] if high is None else high
        delay = random.uniform(low, high)
        time.sleep(delay)
        return delay

    def verify_cookie(self):
        """验证 Cookie 是否有效"""
        url = f"{MOBILE_API_BASE}/api/config"
        headers = self._headers()
        status, body = http_get(url, headers)
        if status != 200:
            return False
        data = json.loads(body)
        self.uid = data.get('data', {}).get('uid', '')
        return data.get('data', {}).get('login', False)

    def fetch_screen_name(self):
        """获取当前用户的昵称"""
        if not self.uid:
            return
        url = (f"{MOBILE_API_BASE}/api/container/getIndex?"
               f"type=uid&value={self.uid}&containerid=100505{self.uid}")
        headers = self._headers()
        try:
            status, body = http_get(url, headers)
            if status == 200:
                data = json.loads(body)
                self.screen_name = data.get('data', {}).get('userInfo', {}).get('screen_name', '')
        except Exception:
            pass

    def fetch_chaohua_list(self):
        """获取全部超话列表(移动端 API, 分页)"""
        collected = []
        since_id = None
        page = 1
        max_pages = 10

        while page <= max_pages:
            log(f"正在获取第 {page} 页超话列表...")
            params = {'containerid': '100803_-_followsuper'}
            if since_id:
                params['since_id'] = since_id

            url = f"{MOBILE_API_BASE}/api/container/getIndex?{urlencode(params)}"
            headers = self._headers()

            status, body = http_get(url, headers)
            if status != 200:
                raise Exception(f"HTTP Error: {status}")

            data = json.loads(body)
            if data.get('ok') != 1:
                error_msg = data.get('msg', '未知错误')
                raise Exception(f"API返回错误: {error_msg}")

            cards = data.get('data', {}).get('cards', [])
            if not cards:
                break

            for card in cards:
                card_group = card.get('card_group', [])
                for item in card_group:
                    topic_name = item.get('title_sub', '')
                    buttons = item.get('buttons', [])
                    if not topic_name or not buttons:
                        continue

                    # 从 buttons 中找签到按钮
                    for btn in buttons:
                        btn_name = btn.get('name', '')
                        btn_scheme = btn.get('scheme', '')

                        if btn_name == '签到' and btn_scheme:
                            collected.append({
                                'name': topic_name,
                                'scheme': btn_scheme,
                                'already_signed': False,
                            })
                            break
                        elif btn_name in ('已签', '已签到', '明日再来'):
                            collected.append({
                                'name': topic_name,
                                'scheme': '',
                                'already_signed': True,
                            })
                            break

            # 检查是否有下一页
            since_id = data.get('data', {}).get('cardlistInfo', {}).get('since_id')
            if not since_id:
                break

            page += 1
            time.sleep(random.uniform(1.0, 2.0))

        if page > max_pages:
            log(f"⚠️ 已达最大翻页数({max_pages})，停止获取", 'WARNING')

        return collected

    def sign_chaohua(self, topic_name, scheme):
        """签到单个超话(使用服务端返回的 scheme URL)"""
        if not scheme:
            return {'success': False, 'msg': '无签到URL', 'already_signed': False}

        url = scheme if scheme.startswith('http') else f"{MOBILE_API_BASE}{scheme}"

        headers = self._headers()

        try:
            status, body = http_get(url, headers)
            if status != 200:
                return {'success': False, 'msg': f'HTTP错误: {status}', 'already_signed': False}

            data = json.loads(body)
            if data.get('ok') == 1:
                msg = data.get('data', {}).get('msg', '签到成功')
                return {'success': True, 'msg': msg, 'already_signed': False}
            else:
                msg = data.get('data', {}).get('msg', data.get('msg', '未知错误'))
                # 检查是否已签到
                if '已签' in msg or '已签到' in msg:
                    return {'success': True, 'msg': msg, 'already_signed': True}
                return {'success': False, 'msg': msg, 'already_signed': False}
        except Exception as e:
            return {'success': False, 'msg': f'签到失败: {str(e)}', 'already_signed': False}

    def run(self):
        """单个账户执行签到"""
        # 先验证 Cookie
        if not self.verify_cookie():
            log("Cookie 已过期或无效，请重新获取", 'ERROR')
            return {'success': False, 'total': 0, 'success_count': 0,
                    'already_signed_count': 0, 'fail_count': 0,
                    'detail': [], 'error': 'Cookie无效或已过期'}

        # 获取用户名
        self.fetch_screen_name()
        user_info = self.get_user_info()
        log(f"🚀 开始执行签到任务 ({user_info})")

        try:
            log("📋 正在获取超话列表...")
            chaohua_list = self.fetch_chaohua_list()

            if not chaohua_list:
                log("未获取到超话列表，请检查Cookie是否有效", 'WARNING')
                return {'success': False, 'total': 0, 'success_count': 0,
                        'already_signed_count': 0, 'fail_count': 0,
                        'detail': [], 'error': '未获取到超话列表'}

            # 按关键词过滤超话
            filter_topics = self.config.get('topics', [])
            if filter_topics:
                before = len(chaohua_list)
                chaohua_list = [c for c in chaohua_list
                                if any(kw in c['name'] for kw in filter_topics)]
                log(f"🔍 已过滤: {before} → {len(chaohua_list)} 个超话 "
                    f"(关键词: {', '.join(filter_topics)})")
                if not chaohua_list:
                    log("过滤后无匹配超话，请检查 WEIBO_TOPICS 配置", 'WARNING')
                    return {'success': False, 'total': 0, 'success_count': 0,
                            'already_signed_count': 0, 'fail_count': 0,
                            'detail': [], 'error': '过滤后无匹配超话'}

            log(f"📊 成功获取到 {len(chaohua_list)} 个超话")

            success_count = 0
            already_signed_count = 0
            fail_count = 0
            detail = []

            for i, chaohua in enumerate(chaohua_list, 1):
                chaohua_name = chaohua['name']

                # 已签到的直接跳过(不发请求)
                if chaohua['already_signed']:
                    log(f"[{chaohua_name}] 今日已签到", 'WARNING')
                    already_signed_count += 1
                    detail.append(f"⚠️ {chaohua_name}：今日已签到")
                    continue

                log(f"📝 正在签到 ({i}/{len(chaohua_list)}): {chaohua_name}")
                result = self.sign_chaohua(chaohua_name, chaohua['scheme'])

                if result['success']:
                    if result.get('already_signed'):
                        log(f"[{chaohua_name}] {result['msg']}", 'WARNING')
                        already_signed_count += 1
                        detail.append(f"⚠️ {chaohua_name}：{result['msg']}")
                    else:
                        log(f"[{chaohua_name}] {result['msg']}", 'SUCCESS')
                        success_count += 1
                        detail.append(f"✅ {chaohua_name}：{result['msg']}")
                else:
                    log(f"[{chaohua_name}] {result['msg']}", 'ERROR')
                    fail_count += 1
                    detail.append(f"❌ {chaohua_name}：{result['msg']}")

                if i < len(chaohua_list):
                    used = self._sleep_random()
                    log(f"   等待 {used:.1f} 秒后继续...")

            log("=" * 30)
            log("📈 签到统计结果:")
            log(f"✅ 签到成功: {success_count} 个")
            log(f"⚠️  已签过: {already_signed_count} 个")
            log(f"❌ 签到失败: {fail_count} 个")
            log(f"📊 总计处理: {len(chaohua_list)} 个超话")

            if success_count > 0 or already_signed_count > 0:
                log("🎉 账户签到任务完成!", 'SUCCESS')
            else:
                log("没有成功签到任何超话，请检查Cookie状态", 'WARNING')

            return {
                'success': True,
                'total': len(chaohua_list),
                'success_count': success_count,
                'already_signed_count': already_signed_count,
                'fail_count': fail_count,
                'detail': detail,
            }

        except Exception as e:
            log(f"任务执行失败: {str(e)}", 'ERROR')
            if 'cookie' in str(e).lower() or 'login' in str(e).lower():
                log("💡 建议: 请重新获取微博Cookie并更新环境变量", 'INFO')
            return {
                'success': False, 'total': 0, 'success_count': 0,
                'already_signed_count': 0, 'fail_count': 0,
                'detail': [], 'error': str(e),
            }


# ==================== Cookie 解析(支持多账户) ====================
def parse_cookies(context):
    cookies_env = get_env(context, 'WEIBO_COOKIES', '')
    if cookies_env:
        if '@' in cookies_env:
            cookies = [c.strip() for c in cookies_env.split('@') if c.strip()]
        elif '\n' in cookies_env:
            cookies = [c.strip() for c in cookies_env.split('\n') if c.strip()]
        elif '----' in cookies_env:
            cookies = [c.strip() for c in cookies_env.split('----') if c.strip()]
        else:
            cookies = [cookies_env.strip()]
        if cookies:
            log(f"🔍 检测到多账户配置，共 {len(cookies)} 个账户")
            return cookies

    cookie_env = get_env(context, 'WEIBO_COOKIE', '')
    if cookie_env:
        log("🔍 检测到单账户配置")
        return [cookie_env.strip()]

    return []


# ==================== WxPusher 通知 ====================
WXPUSHER_API = 'https://wxpusher.zjiecode.com/api/send/message'


def wxpusher_notify(app_token, uid_list, topic_id, title, content_html, summary):
    if not app_token or (not uid_list and not topic_id):
        return
    try:
        payload = {
            'appToken': app_token,
            'content': content_html,
            'summary': summary[:90],
            'contentType': 2,
        }
        if uid_list:
            payload['uids'] = uid_list
        if topic_id:
            payload['topicIds'] = [int(topic_id)]

        status, body = http_post_json(WXPUSHER_API, payload)
        result = json.loads(body)
        if result.get('code') == 1000:
            target = f"{len(uid_list)}个UID" if uid_list else ""
            if topic_id:
                target = f"主题{topic_id}的全部订阅者" if not target else f"{target}+主题{topic_id}"
            log(f"✅ WxPusher 通知发送成功 -> {target}", 'SUCCESS')
        else:
            log(f"WxPusher 通知发送失败: {result.get('msg', '未知错误')}", 'WARNING')
    except Exception as e:
        log(f"发送 WxPusher 通知时出错: {str(e)}", 'WARNING')


# ==================== 主流程 ====================
def run_all(context, event=None):
    config = build_config(context, event=event)
    cookies = parse_cookies(context)

    if not cookies:
        log("❌ 请设置环境变量 WEIBO_COOKIE 或 WEIBO_COOKIES", 'ERROR')
        return {'ok': False, 'error': '未配置 WEIBO_COOKIE / WEIBO_COOKIES'}

    total_accounts = len(cookies)
    all_results = []
    log(f"🎯 开始执行批量签到任务，共 {total_accounts} 个账户")

    for i, cookie in enumerate(cookies, 1):
        if not cookie or len(cookie) < 50:
            log(f"❌ 账户{i} Cookie无效，跳过", 'ERROR')
            all_results.append({'account': i, 'result': {
                'success': False, 'total': 0, 'success_count': 0,
                'already_signed_count': 0, 'fail_count': 0,
                'detail': [], 'error': 'Cookie无效(长度不足)'}})
            continue

        signin = WeiboChaohuaSignin(cookie, config, i, total_accounts)
        result = signin.run()
        all_results.append({
            'account': i,
            'screen_name': signin.screen_name or f"账户{i}",
            'result': result,
        })

        if i < total_accounts:
            acc_delay = config['account_interval'] * random.uniform(0.8, 1.5)
            log(f"⏱️  等待 {acc_delay:.0f} 秒后处理下一个账户...")
            time.sleep(acc_delay)

    # 汇总统计
    total_success = sum(r['result'].get('success_count', 0) for r in all_results)
    total_already = sum(r['result'].get('already_signed_count', 0) for r in all_results)
    total_fail = sum(r['result'].get('fail_count', 0) for r in all_results)
    total_topics = sum(r['result'].get('total', 0) for r in all_results)
    success_accounts = sum(1 for r in all_results if r['result'].get('success'))

    summary = {
        'ok': success_accounts > 0,
        'accounts': total_accounts,
        'success_accounts': success_accounts,
        'total_topics': total_topics,
        'success_count': total_success,
        'already_signed_count': total_already,
        'fail_count': total_fail,
    }
    log(f"📊 总体统计: 账户{success_accounts}/{total_accounts} | "
        f"新签{total_success} | 已签{total_already} | 失败{total_fail} | 共{total_topics}")

    # WxPusher 推送
    app_token = config['wxpusher_token']
    uid_list = config['wxpusher_uids']
    topic_id = config['wxpusher_topic']
    if app_token and (uid_list or topic_id):
        lines = []
        for r in all_results:
            result = r['result']
            name = r.get('screen_name', f"账户{r['account']}")
            if result.get('success'):
                lines.append(f"<p><b>{name}</b>：新签 {result['success_count']} | "
                             f"已签 {result['already_signed_count']} | "
                             f"失败 {result['fail_count']} | 共 {result['total']}</p>")
                lines.extend(f"<p style='margin:2px 0;font-size:12px;color:#666'>{d}</p>"
                             for d in result.get('detail', [])[:50])
            else:
                lines.append(f"<p><b>{name}</b>：执行失败 - "
                             f"{result.get('error', '未知错误')}</p>")
        cookie_errors = [r for r in all_results
                         if not r['result'].get('success')
                         and 'cookie' in str(r['result'].get('error', '')).lower()]
        if cookie_errors:
            title = f"⚠️ Cookie过期({len(cookie_errors)}个账户) - 微博超话签到报告"
            summary_text = (f"⚠️ {len(cookie_errors)}个账户Cookie过期! "
                            f"新签{total_success} 已签{total_already} "
                            f"失败{total_fail} (共{total_topics}, "
                            f"账户{success_accounts}/{total_accounts})")
        else:
            title = '微博超话签到报告'
            summary_text = (f"签到完成: 新签{total_success} 已签{total_already} "
                            f"失败{total_fail} (共{total_topics}, "
                            f"账户{success_accounts}/{total_accounts})")
        wxpusher_notify(app_token, uid_list, topic_id, title,
                        ''.join(lines) or '<p>无结果</p>', summary_text)

    return summary


# ==================== FunctionGraph 入口 ====================
def handler(event, context):
    try:
        config = build_config(context, event=event)
        if config['start_jitter_max'] > 0:
            jitter = random.uniform(0, config['start_jitter_max'])
            log(f"🎲 随机等待 {jitter:.0f} 秒后开始签到(启动抖动防风控)")
            time.sleep(jitter)

        return run_all(context, event=event)
    except Exception as e:
        log(f"❌ 程序执行异常: {str(e)}", 'ERROR')
        return {'ok': False, 'error': str(e)}


# ==================== 本地调试入口 ====================
if __name__ == '__main__':
    start_time = time.time()
    result = run_all(None)
    duration = int(time.time() - start_time)
    print(f"⏱️  总耗时: {duration} 秒")
    print(json.dumps(result, ensure_ascii=False, indent=2))
