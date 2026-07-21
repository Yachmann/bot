import os
import re
import json
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ===================== CONFIGURAÇÃO =====================
URL = "https://buyticketbrasil.com/evento/harrystylestogethertogether2026?data=1784948340000&evento_local=1769181690010x821473434180517900&cidade=S%C3%A3o+Paulo"

# No Render, configure essas variáveis em Environment (não deixe hardcoded no código/git)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PRICE_THRESHOLD_REAIS = float(os.environ.get("PRICE_THRESHOLD_REAIS", 330.0))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 60))

# Render injeta a porta que o serviço precisa escutar nessa variável
PORT = int(os.environ.get("PORT", 10000))

# Categorias de ingresso aceitas
ALLOWED_CATEGORIAS = {"pit circle", "pit disco", "pit square", "pit kiss"}

# Tipos de ingresso aceitos (dentro das categorias acima)
ALLOWED_TIPOS = {"meia estudante", "inteira"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError(
        "TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID não configurados. "
        "No Render, defina essas variáveis em Settings > Environment."
    )

already_alerted = set()
last_heartbeat = 0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


# ----------------------------------------------------------------------
# SERVIDOR HTTP "FAKE" — só existe pra:
#   1) o Render aceitar isso como Web Service (ele espera algo escutando
#      na porta indicada por $PORT);
#   2) dar um endereço pro serviço externo de ping (ex: cron-job.org,
#      UptimeRobot) bater a cada ~10 minutos e evitar o "sleep" de 15 min
#      do plano gratuito.
# Ele não faz nada além de responder "OK" — toda a lógica real do bot
# continua rodando no loop principal, em uma thread separada.
# ----------------------------------------------------------------------

class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot de ingressos rodando.")

    def log_message(self, format, *args):
        # silencia o log de cada request HTTP (senão polui o log do bot)
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), _HealthCheckHandler)
    log.info(f"Servidor HTTP fake escutando na porta {PORT} (health check).")
    server.serve_forever()


def is_target_key(key: str) -> bool:
    """
    Retorna True se a 'key' (formato 'Categoria||Tipo') pertence a uma das
    categorias em ALLOWED_CATEGORIAS (Pit Circle, Pit Disco, Pit Square,
    Pit Kiss) E o tipo do ingresso for 'Inteira' ou 'Meia Estudante'
    (comparação case-insensitive).
    """
    if "||" not in key:
        return False

    categoria, tipo = key.split("||", 1)

    categoria_ok = categoria.strip().lower() in ALLOWED_CATEGORIAS
    tipo_ok = tipo.strip().lower() in ALLOWED_TIPOS

    return categoria_ok and tipo_ok


def send_hourly_status():
    global last_heartbeat

    now = time.time()

    # Envia apenas uma vez por hora
    if now - last_heartbeat < 3600:
        return

    matriz = fetch_matriz_preco()

    cheapest_key = None
    cheapest_price = None
    cheapest_available = 0

    for key, info in matriz.items():
        if not is_target_key(key):
            continue

        preco = info.get("preco_min")
        if preco is None:
            continue

        if cheapest_price is None or preco < cheapest_price:
            cheapest_price = preco
            cheapest_key = key
            cheapest_available = info.get("disponivel", 0)

    if cheapest_key:
        preco_reais = cheapest_price / 100

        msg = (
            "✅ <b>Bot ativo há mais uma hora.</b>\n\n"
            f"🎫 Menor preço encontrado (Pit Circle/Disco/Square/Kiss - Inteira/Meia Estudante):\n"
            f"{cheapest_key.replace('||', ' - ')}\n"
            f"💰 R${preco_reais:.2f}\n"
            f"📦 Disponíveis: {cheapest_available}"
        )
    else:
        msg = "✅ Bot ativo há mais uma hora.\nNenhum ingresso Pit Circle/Disco/Square/Kiss (Inteira/Meia Estudante) encontrado."

    send_telegram_message(msg)
    last_heartbeat = now


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, data=payload, timeout=10)
        resp.raise_for_status()
        log.info("Mensagem enviada ao Telegram.")
    except Exception as e:
        log.error(f"Falha ao enviar mensagem no Telegram: {e}")


def extract_balanced(text: str, start_index: int, open_ch="{", close_ch="}") -> str:
    """A partir de start_index (que deve apontar pro caractere de abertura),
    retorna a substring balanceada até o fechamento correspondente."""
    assert text[start_index] == open_ch
    depth = 0
    for i in range(start_index, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[start_index:i + 1]
    raise ValueError("Não foi possível encontrar o fechamento balanceado.")


def unescape_js_string(raw: str) -> str:
    """O JSON vem embutido dentro de uma string JS (com aspas escapadas)."""
    return raw.replace('\\"', '"').replace("\\\\", "\\")


def fetch_matriz_preco():
    resp = requests.get(URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text

    # localiza o campo "matriz_preco" dentro do payload (ainda escapado com \")
    marker = '\\"matriz_preco\\":'
    idx = html.find(marker)
    if idx == -1:
        raise RuntimeError("Campo 'matriz_preco' não encontrado no HTML — o site pode ter mudado a estrutura.")

    brace_start = html.find("{", idx)
    raw_block = extract_balanced(html, brace_start, "{", "}")
    unescaped = unescape_js_string(raw_block)

    return json.loads(unescaped)


def check_tickets():
    matriz = fetch_matriz_preco()
    log.info(f"{len(matriz)} combinações de ingresso encontradas no total.")

    for key, info in matriz.items():
        if not is_target_key(key):
            continue

        preco_centavos = info.get("preco_min")
        disponivel = info.get("disponivel", 0)
        if preco_centavos is None:
            continue

        preco_reais = preco_centavos / 100
        log.info(f"{key} -> R${preco_reais:.2f} ({disponivel} disponíveis)")

        if preco_reais < PRICE_THRESHOLD_REAIS and disponivel > 0:
            alert_key = f"{key}-{preco_centavos}"
            if alert_key not in already_alerted:
                msg = (
                    f"🎫 <b>Ingresso abaixo de R${PRICE_THRESHOLD_REAIS:.0f}!</b>\n"
                    f"Tipo: {key.replace('||', ' - ')}\n"
                    f"Preço: R${preco_reais:.2f}\n"
                    f"Disponíveis: {disponivel}\n"
                    f"Link: {URL}"
                )
                send_telegram_message(msg)
                already_alerted.add(alert_key)


def main():
    # Sobe o servidor HTTP fake em background, pro Render enxergar o serviço
    # como "no ar" e pro ping externo ter algo pra acertar.
    threading.Thread(target=start_health_server, daemon=True).start()

    while True:
        try:
            send_hourly_status()   # envia status se passou 1 hora
            check_tickets()        # verifica promoções
        except Exception as e:
            log.error(f"Erro no ciclo de verificação: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
