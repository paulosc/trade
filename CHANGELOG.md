# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) · Versões: [SemVer](https://semver.org/lang/pt-BR/).

## [0.5.0] - 2026-10-28

### Adicionado
- `trade-bot` v0.2.0: painel local no navegador (`python -m tradebot painel` ou dois cliques em `trade-bot/Abrir painel.bat`) com passo a passo para ligar o robô em `sim` (sem conta) ou `paper` (Alpaca), testar as chaves, escolher as ações, ligar/desligar, zerar, kill switch e acompanhar o dia em português (REQ-13..16).
- Travas do painel: só `sim`/`paper`, só `127.0.0.1`, token por sessão e chaves só em `trade-bot/.env` (SEC-07..09). ADR 0002.

### Alterado
- SPEC: no Windows é preciso `pip install tzdata` (o atalho do painel instala sozinho).

## [0.4.0] - 2026-10-28

### Adicionado
- Vendor: `impeccable@d34c87e1a009` (pack `design`), `taste-skill@12af471f627f` (pack `taste`), `google-maps-scraper-kit@656d66daa269` (fora dos packs, com aviso legal).
- `scripts/leis.sh` + `docs/leis-brasil.md`: leis do Brasil (ctresb/leis) por sparse checkout.
- `docs/leads-google-maps.md` (Termos do Google, LGPD, telemetria) e `docs/higgsfield.md` (serviço pago).

## [0.3.0] - 2026-10-28

### Adicionado
- `trade-bot` v0.1.1: demonstração offline `trade-bot/demo/replay.py` (pregão sintético com 3 ativos).
- Vídeo explicativo gerado com HyperFrames (`trade-bot/demo/video/build.py`) e prints em `docs/demo/`.

### Corrigido
- `trade-bot`: a tolerância de preço da entrada podia superar o próprio risco da operação; agora é limitada a 20% do risco (REQ-12).

## [0.2.0] - 2026-10-28

### Adicionado
- Vendor: `brag@669ab9286aa2` (skill `/brag`, vídeo de lançamento do projeto via HyperFrames), no pack `brag` do `install-skills.sh`.

### Alterado
- Harness: versão do toolkit (`VERSION`) desacoplada da versão do `trade-bot` (que segue em `trade-bot` v0.1.0).

## [0.1.0] - 2026-10-28

### Adicionado
- `trade-bot` v0.1.0: robô ORB + Máquina do Tempo + regras de risco para a Alpaca (modos `sim`, `paper` e `live`), CLI, backtest walk-forward e 49 testes offline. SPEC em `trade-bot/docs/SPEC.md`.
- Vendor: `marketingskills@0cd9b3a0ef4c`, `hyperframes/skills@439d6856780f`, `awesome-design-md@3b605329240c`.
- Scripts: `harness.sh`, `install-skills.sh`, `use-design.sh`, `update-vendor.sh`.
- CI no GitHub Actions rodando o harness em Python 3.10, 3.12 e 3.13.
- Guia `docs/motion-graphics.md`.
