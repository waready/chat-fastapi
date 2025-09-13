import os, json, asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiofiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path

# ====== Config ======
ROOT = Path(__file__).parent 
DB_FILE = "chat.json"
TMP_FILE = DB_FILE + ".tmp"
HISTORY_ON_CONNECT = 0   # ¡clave! No mandamos historial por WS (el front lo pide por REST)

app = FastAPI(title="Chat WS (compact)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====== Util ======
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

async def init_db():
    if not os.path.exists(DB_FILE):
        async with aiofiles.open(DB_FILE, "w", encoding="utf-8") as f:
            await f.write("[]\n")

async def load_messages() -> List[dict]:
    try:
        async with aiofiles.open(DB_FILE, "r", encoding="utf-8") as f:
            data = (await f.read()).strip()
        return json.loads(data) if data else []
    except Exception:
        await save_messages([])
        return []

async def save_messages(messages: List[dict]):
    async with aiofiles.open(TMP_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(messages, ensure_ascii=False, indent=2))
    os.replace(TMP_FILE, DB_FILE)

async def append_message(record: dict):
    msgs = await load_messages()
    msgs.append(record)
    await save_messages(msgs)

def is_user_message(texto: str) -> bool:
    return bool(texto and texto.startswith("[") and "]" in texto)

# ====== Estado ======
clientes: Dict[WebSocket, str] = {}  # ws -> nombre
lock = asyncio.Lock()

async def broadcast(texto: str, author: Optional[str] = None, omit: Optional[WebSocket] = None):
    rec = {"text": texto, "author": author or "system", "ts": now_iso()}
    try:
        await append_message(rec)
    except Exception as e:
        print("Persistencia falló:", e)

    # snapshot de destinatarios
    async with lock:
        dests = [ws for ws in clientes.keys() if ws is not omit]

    # enviar
    fallen = []
    for ws in dests:
        try:
            await ws.send_text(rec["text"])
        except Exception:
            fallen.append(ws)
    if fallen:
        async with lock:
            for ws in fallen:
                clientes.pop(ws, None)

# ====== App hooks ======
@app.on_event("startup")
async def _startup():
    await init_db()

# ====== WebSocket ======
@app.websocket("/ws")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    nombre = ""
    try:
        # Primer mensaje = nombre (si no, autogen)
        try:
            nombre = (await ws.receive_text()).strip()
        except Exception:
            nombre = ""
        if not nombre:
            peer = ws.client
            nombre = f"user_{peer.host}:{peer.port}" if peer else f"user_{id(ws)}"

        # Registrar y avisar unión
        async with lock:
            clientes[ws] = nombre
        await broadcast(f"🟢 {nombre} se unió al chat", author="system")

        # Loop de mensajes
        while True:
            msg = (await ws.receive_text()).strip()
            if not msg:
                continue
            # Si no quieres eco al emisor, usa omit=ws
            await broadcast(f"[{nombre}] {msg}", author=nombre)  # , omit=ws

    except WebSocketDisconnect:
        pass
    finally:
        async with lock:
            alias = clientes.pop(ws, None)
        if alias:
            await broadcast(f"🔴 {alias} salió del chat", author="system", omit=ws)

# ====== REST: historial ======
@app.get("/messages")
async def get_messages(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    only_user: bool = Query(False, description="Si true: solo mensajes '[Nombre] ...'"),
):
    msgs = await load_messages()
    if only_user:
        msgs = [m for m in msgs if is_user_message(m.get("text", ""))]
    total = len(msgs)
    items = msgs[offset: offset + limit]
    return JSONResponse({"total": total, "offset": offset, "limit": limit, "items": items})

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(str(ROOT / "index.html"), media_type="text/html")