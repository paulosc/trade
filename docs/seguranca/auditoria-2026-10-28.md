# Auditoria de segurança — trade-bot, painel local e chaves (rodada 1)

## 1. Escopo e como a rodada foi feita
- **Perfil:** `quick` (uma onda de caça + um crítico final; cada suspeita com um verificador independente). **Rodada parcial**, não cobertura completa.
- **Escopo:** painel local (`trade-bot/tradebot/painel.py`, `painel.html`), manuseio das chaves (`.env`, ambiente do robô, respostas da API) e o atalho `trade-bot/Abrir painel.bat`. Fora do escopo, registrados no ledger: linha de comando e modo live, motor e regras de risco, skills vendorizadas, e a releitura de `state_dir` pelo robô.
- **Fonte:** `paulosc/trade` em `f2ba70e`, worktree limpo.
- **Orçamento:** 14 agentes; usados 9 (4 reconhecimento, 3 caçadores, 1 crítico, 1 verificador).
- **Execução:** só leitura do código. O host Windows não oferece sandbox imposto pelo SO e nenhuma imagem local confiável tem Python; nenhum código do alvo foi executado, e o painel em funcionamento não foi tocado.
- **Rodadas anteriores:** nenhuma (primeiro ledger).
- **Adiado pelo perfil quick** (o crítico apontou, não houve segunda onda): (a) autenticação do par local — qualquer processo da máquina lê o token em `GET /`, inclusive o caso de porta ocupada por impostor; (b) ciclo de vida do painel e do robô — robô órfão ao fechar a janela, segundo robô ao reabrir, troca para `sim` com o robô rodando.

## 2. Postura
Para os atacantes que importam neste cenário — **sites abertos no navegador do dono** — o painel está bem protegido: escuta só em 127.0.0.1, confere o cabeçalho Host (barra DNS rebinding), exige um token aleatório em todo `/api/*` (barra CSRF), nega enquadramento (clickjacking) e escapa o que vai para a página. A trava do **modo live** (SEC-07) resiste a todos os caminhos analisados, inclusive à troca do `config.json` entre a checagem e o início do robô. As **chaves** nunca voltam ao navegador nem aparecem em erros, logs ou saída do robô.

## 3. Falhas confirmadas
Nenhuma.

| Severidade | Título | Fronteira | Resultado observado |
|---|---|---|---|
| — | — | — | — |

## 4. Detalhe das falhas confirmadas
Não há.

## 5. NEEDS VALIDATION
Nenhuma pista ficou pendente. A única suspeita (`trade-bot/painel/env-tmp-not-gitignored`) foi **rejeitada** pelo verificador independente: só o dono, com o token, salva as chaves; o temporário ficaria na pasta do próprio dono; a chave só chegaria ao GitHub se o próprio dono commitasse um arquivo novo listado no `git status`. Ficou como nota de reforço abaixo.

## 6. Reforços sugeridos (não são falhas)
Prioridade sugerida, da mais útil para a menos:
1. **Ciclo de vida do robô:** garantir que fechar a janela do painel desliga o robô (ou impedir um segundo robô com uma trava de instância única). Ver a pista adiada (b).
2. **`.env.tmp`:** em `write_env_file`, usar `tempfile.mkstemp` na mesma pasta e apagar o temporário num `finally`; acrescentar `.env*` e `*.tmp` ao `.gitignore`; teste citando SEC-09.
3. **Permissão do `.env`:** restringir a ACL à conta do dono ao criar o arquivo (o código hoje herda as permissões da pasta; não verificado nesta rodada).
4. **Kill switch:** aceitar só booleano JSON em `/api/kill` (um corpo sem `on` desliga o kill); capturar também `OSError` no zerar.
5. **Cabeçalhos:** `X-Content-Type-Options: nosniff` e `Content-Security-Policy` com `frame-ancestors 'none'`.
6. **Página:** passar por `esc()`/`Number()` os números do topo do radar e escapar também a aspa simples em `esc()`.
7. **Robustez HTTP:** validar tipos (token ASCII, corpo JSON objeto) para responder 400/403 limpo; `Handler.timeout = 10`.
8. **`check_paper_keys`:** recusar caracteres de controle e não ecoar `str(e)` de `ValueError`.
9. **Dados e atalho:** `quote(symbol, safe='')` e validar símbolos também em `load_config`; tirar a entrada vazia do `PYTHONPATH`; remover `ALPACA_DATA_*` do ambiente do robô ou documentar; no `.bat`, chamar o Python por caminho absoluto (ou `py -3`) e fixar a versão do `tzdata`.

**Padrões positivos no código:** `secrets.token_urlsafe` + `compare_digest`; allowlist exata de Host; bind só em loopback; `X-Frame-Options: DENY`, `no-store`, `no-referrer`; chaves testadas na Alpaca antes de salvar; recusa de chave `AK` (live); quatro camadas contra o modo live; `Alpaca.__repr__` esconde as chaves; log HTTP silenciado; radar só faz GET.

## 7. Cobertura (do ledger)
- Cobertas: **12** · Candidata (rejeitada na validação): **1** · Bloqueadas: 0 · Adiadas: **2** (perfil quick) · Fora do escopo: **4**.
- Crítico final: propôs 2 unidades novas (adiadas) e 1 fora do escopo; nenhuma reatribuição.
- Recomendado: uma rodada `standard` focada nas duas unidades adiadas, de preferência com um sandbox (por exemplo, imagem Docker oficial de Python com rede desligada) para validar comportamentos de execução.
