import os
import re
import json
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ===================== CONFIGURAÇÃO =====================
URL = "https://www.ticketmaster.com.br/event/venda-geral-harry-styles"

# No Render, configure essas variáveis em Environment (não deixe hardcoded no código/git)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_IDS = os.environ.get(
    "TELEGRAM_CHAT_IDS",
    ""
).split(",")


PRICE_THRESHOLD_REAIS = float(os.environ.get("PRICE_THRESHOLD_REAIS", 150.0))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", 60))

# De quanto em quanto tempo o bot manda a mensagem "ainda estou rodando" (em segundos).
# Padrão: 3600 * 6 = 21600s = 6 horas.
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", 3600 * 6))

# Render injeta a porta que o serviço precisa escutar nessa variável
PORT = int(os.environ.get("PORT", 10000))

# Só setores que começam com "Pit" (Pit Circle, Pit Disco, Pit Square, Pit Kiss)
SETOR_PREFIXO = "pit"

# No site da Ticketmaster não existe uma categoria separada "Meia Estudante":
# existe uma única categoria genérica "Meia-Entrada" (cobre estudante, jovem
# baixa renda, professor e aposentado), separada de "Meia-Entrada PCD" e do
# "Desc. 50% - Estatuto Idoso". É essa "Meia-Entrada" genérica que cobre
# estudante nesse site, e é ela que o bot monitora.
TIPO_ALVO = "meia-entrada"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

if not TELEGRAM_TOKEN or not any(TELEGRAM_CHAT_IDS):
    raise RuntimeError(
        "TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_IDS não configurados."
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
        self.wfile.write(b"Bot de ingressos (Harry Styles - Ticketmaster) rodando.")

    def log_message(self, format, *args):
        # silencia o log de cada request HTTP (senão polui o log do bot)
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), _HealthCheckHandler)
    log.info(f"Servidor HTTP fake escutando na porta {PORT} (health check).")
    server.serve_forever()


def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    for chat_id in TELEGRAM_CHAT_IDS:
        chat_id = chat_id.strip()
        if not chat_id:
            continue

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            resp = requests.post(url, data=payload, timeout=10)
            resp.raise_for_status()
            log.info(f"Mensagem enviada para {chat_id}.")
        except Exception as e:
            log.error(f"Falha ao enviar para {chat_id}: {e}")


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


def fetch_bootstrap_data() -> dict:
    """
    A página da Ticketmaster carrega os dados do evento num bloco JS:
        App.bootstrapData({...JSON...});
    Diferente do BuyTicket, aqui o JSON já vem "limpo" (sem escaping extra),
    então basta extrair o objeto balanceado e usar json.loads direto.
    """
    resp = requests.get(URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text

    marker = "App.bootstrapData("
    idx = html.find(marker)
    if idx == -1:
        raise RuntimeError("Bloco 'App.bootstrapData(' não encontrado — a página pode ter mudado a estrutura.")

    brace_start = html.find("{", idx)
    raw_block = extract_balanced(html, brace_start, "{", "}")
    return json.loads(raw_block)


def get_pit_meia_entrada_tickets():
    """
    Retorna uma lista de dicts com os ingressos dos setores Pit (Circle,
    Disco, Square, Kiss) do tipo 'Meia-Entrada', de todas as datas/shows
    disponíveis na página:
        {"show": ..., "setor": ..., "preco": ..., "disponivel": bool}
    """
    data = fetch_bootstrap_data()
    shows = data.get("model", {}).get("data", {}).get("shows", [])

    resultados = []
    for show in shows:
        show_name = show.get("name", "")
        for sector in show.get("sectors", []):
            nome_setor = sector.get("name", "")
            if not nome_setor.strip().lower().startswith(SETOR_PREFIXO):
                continue

            for rate in sector.get("rates", []):
                nome_tipo = rate.get("name", "")
                if nome_tipo.strip().lower() != TIPO_ALVO:
                    continue

                resultados.append({
                    "show": show_name,
                    "setor": nome_setor,
                    "tipo": nome_tipo,
                    "preco": rate.get("price"),
                    "disponivel": bool(rate.get("available", False)),
                })

    return resultados


def send_hourly_status():
    global last_heartbeat

    now = time.time()

    # Envia apenas a cada HEARTBEAT_INTERVAL_SECONDS
    if now - last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
        return

    tickets = get_pit_meia_entrada_tickets()

    cheapest = None
    for t in tickets:
        if t["preco"] is None:
            continue
        if cheapest is None or t["preco"] < cheapest["preco"]:
            cheapest = t

    if cheapest:
        msg = (
            "✅ <b>Bot ativo (Harry Styles - Ticketmaster).</b>\n\n"
            f"🎫 Menor preço encontrado (Pit - Meia-Entrada):\n"
            f"{cheapest['setor']} - {cheapest['tipo']}\n"
            f"💰 R$ {cheapest['preco']:.2f}\n"
            f"📦 Disponível: {'Sim' if cheapest['disponivel'] else 'Não'}"
        )
    else:
        msg = "✅ Bot ativo (Harry Styles - Ticketmaster).\nNenhum ingresso Pit Meia-Entrada encontrado."

    send_telegram_message(msg)
    last_heartbeat = now


def check_tickets():
    tickets = get_pit_meia_entrada_tickets()
    log.info(f"{len(tickets)} combinações Pit/Meia-Entrada encontradas no total.")

    for t in tickets:
        preco = t["preco"]
        if preco is None:
            continue

        log.info(f"{t['setor']} - {t['tipo']} -> R$ {preco:.2f} (disponível: {t['disponivel']})")

        if preco < PRICE_THRESHOLD_REAIS and t["disponivel"]:
            alert_key = f"{t['setor']}-{t['tipo']}-{preco}"
            if alert_key not in already_alerted:
                msg = (
                    f"🎫 <b>Ingresso Harry Styles (Pit Meia-Entrada) abaixo de R${PRICE_THRESHOLD_REAIS:.0f}!</b>\n"
                    f"Setor: {t['setor']}\n"
                    f"Tipo: {t['tipo']}\n"
                    f"Preço: R$ {preco:.2f}\n"
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
            send_hourly_status()   # envia status a cada HEARTBEAT_INTERVAL_SECONDS
            check_tickets()        # verifica promoções
        except Exception as e:
            log.error(f"Erro no ciclo de verificação: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
