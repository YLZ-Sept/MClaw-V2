"""
CDP (Chrome DevTools Protocol) 客户端
封装与 Chrome/Edge 浏览器的 WebSocket 通信

依赖: websockets, httpx (Mclaw 后端环境自带)
"""

import json
import asyncio
import base64
import websockets
import httpx
from typing import Optional, Dict, Any, List


class CDPClient:
    """Chrome DevTools Protocol 客户端"""

    def __init__(self, host: str = "localhost", port: int = 9222, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ws_url: Optional[str] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}

    async def connect(self, target_id: Optional[str] = None) -> None:
        """连接到浏览器的指定页面；未指定时选第一个 page 类型 target"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"http://{self.host}:{self.port}/json")
            resp.raise_for_status()
            targets = resp.json()

        if target_id:
            target = next((t for t in targets if t["id"] == target_id), None)
            if not target:
                raise ValueError(f"找不到目标页面: {target_id}")
        else:
            target = next((t for t in targets if t.get("type") == "page"), None)
            if not target and targets:
                target = targets[0]

        if not target:
            raise RuntimeError("没有找到可用的页面，请先打开抖音网页")

        self.ws_url = target["webSocketDebuggerUrl"]
        self.ws = await websockets.connect(self.ws_url, max_size=None)
        asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        """持续接收 WebSocket 消息"""
        try:
            async for msg in self.ws:
                data = json.loads(msg)
                msg_id = data.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if "result" in data:
                        fut.set_result(data["result"])
                    else:
                        fut.set_exception(RuntimeError(data.get("error", "未知错误")))
        except websockets.ConnectionClosed:
            pass

    async def send(self, method: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """发送 CDP 命令并等待结果"""
        self._msg_id += 1
        msg_id = self._msg_id
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params

        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self.ws.send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout=self.timeout)

    async def navigate(self, url: str) -> Dict:
        """导航到指定 URL"""
        return await self.send("Page.navigate", {"url": url})

    async def wait_for_load(self, timeout: float = 30) -> None:
        """等待页面加载完成"""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                result = await self.evaluate("document.readyState")
            except Exception:
                result = None
            if result == "complete":
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("页面加载超时")

    async def evaluate(self, expression: str) -> Any:
        """执行 JavaScript 表达式，返回结果值"""
        # 兼容直接传函数定义（如 "() => {...}"），自动包装为立即执行
        expr = expression.strip()
        if expr.startswith("() =>") or expr.startswith("( ) =>"):
            expr = f"({expr})()"
        result = await self.send("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
        })
        res = result.get("result", {})
        if res.get("type") == "undefined":
            return None
        if res.get("subtype") == "error":
            raise RuntimeError(f"JS 执行错误: {res.get('description', '')}")
        return res.get("value")

    async def screenshot(self, path: Optional[str] = None) -> bytes:
        """截图"""
        result = await self.send("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(result["data"])
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    async def click(self, selector: str) -> bool:
        """通过 CSS 选择器点击元素（先滚动到可视区域）"""
        return bool(await self.evaluate(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return false;
                el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                el.click();
                return true;
            }})()
        """))

    async def focus(self, selector: str) -> bool:
        """聚焦元素"""
        return bool(await self.evaluate(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return false;
                el.focus();
                el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                return true;
            }})()
        """))

    async def set_input_value(self, selector: str, text: str) -> bool:
        """
        在输入框中设置文本（兼容 textarea / contenteditable / input）
        策略: 聚焦 → 全选清空 → 设值 → 派发 input/change 事件
        """
        return bool(await self.evaluate(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return false;
                el.focus();
                el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                // 全选清空（兼容 React 受控组件）
                if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {{
                    el.select();
                    el.value = '';
                }} else {{
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                }}
                // 设置新值
                if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {{
                    el.value = {json.dumps(text)};
                }} else {{
                    el.textContent = {json.dumps(text)};
                }}
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }})()
        """))

    async def press_enter(self) -> None:
        """模拟按下 Enter 键"""
        for key in ("keyDown", "keyUp"):
            await self.send("Input.dispatchKeyEvent", {
                "type": key,
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            })

    async def insert_text(self, text: str) -> None:
        """向当前聚焦元素真实插入文本（模拟 IME 输入，触发 React/Draft.js 状态更新）"""
        await self.send("Input.insertText", {"text": text})

    async def mouse_click(self, x: float, y: float) -> None:
        """在指定页面坐标执行真实鼠标点击（mousedown → mouseup → click）"""
        for etype in ("mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", {
                "type": etype,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            })

    async def get_rect(self, selector: str) -> Optional[Dict]:
        """获取元素在页面中的绝对坐标（用于真实鼠标点击）"""
        return await self.evaluate(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {{x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height}};
            }})()
        """)

    async def scroll_down(self, times: int = 3, pause: float = 1.0) -> None:
        """向下滚动页面，加载更多内容"""
        for _ in range(times):
            await self.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(pause)

    async def close(self) -> None:
        """关闭连接"""
        if self.ws:
            await self.ws.close()
            self.ws = None


async def list_targets(host: str = "localhost", port: int = 9222) -> List[Dict]:
    """列出所有可用的页面 target"""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"http://{host}:{port}/json")
        resp.raise_for_status()
        return resp.json()


async def ping(host: str = "localhost", port: int = 9222) -> bool:
    """检查 CDP 端口是否有浏览器在监听"""
    try:
        targets = await list_targets(host, port)
        return len(targets) > 0
    except Exception:
        return False
