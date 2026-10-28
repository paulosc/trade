# ADR 0002: Painel local no navegador

- Status: Aceita · Data: 2026-10-28

## Contexto

O dono quer ver o robô funcionando e ligá-lo "de verdade" sem decorar comandos de terminal.
A SPEC v0.1 deixava interface gráfica fora de escopo.

## Decisão

- Um painel local servido pelo próprio Python (`http.server`, só biblioteca padrão) e uma página HTML sem dependências externas.
- O painel **não** reimplementa o robô: o botão "Ligar" roda o mesmo `python -m tradebot run` em um processo separado.
- Escopo limitado a `sim` e `paper` (SEC-07). O modo live continua exigindo as três condições da SEC-02, só pela CLI.
- Segurança de servidor local: escuta só em `127.0.0.1`, confere o `Host` e exige token por sessão em `/api/*` (SEC-08).
- Chaves digitadas vão para `trade-bot/.env` (ignorado pelo git) e nunca voltam para a página (SEC-09).

## Consequências

- Quem não usa terminal consegue fazer os 20 pregões em paper exigidos pela SPEC §6.
- Fechar a janela do painel desliga o robô. Para rodar o dia todo sem o computador ligado, continua valendo a recomendação de servidor com `tmux`/`systemd`.
