<div align="center">
  <img src="docs/logo.png" width="96" alt="Simple Limite" />

  # Simple Limite

  Monitor de uso dos planos Claude Code, Codex e Cursor na bandeja do Windows.
</div>

O app consulta o uso dos planos com as credenciais já presentes na máquina e lê o histórico local para mostrar tokens de hoje, total e projetos. Os tokens do histórico são estatísticas: os percentuais de limite vêm dos provedores.

## Informações disponíveis

- **Claude:** janela de 5 horas, limites semanais e por modelo, quando retornados pela API; créditos extras com consumo e teto.
- **Codex:** janelas principal e secundária, incluindo uso semanal, revisão de código e limites adicionais, quando disponíveis. A duração informada pela API define o nome da janela; uma janela de 5 horas não é um limite diário.
- **Codex:** plano, permissão de uso, limite atingido, saldo de créditos extras, resets extras disponíveis e sua primeira expiração, quando informados. O app apenas consulta esses resets.
- **Cursor:** limites do plano pela API e histórico de eventos de uso.
- **Reset:** percentual restante, data/hora local e tempo até a renovação. Um reset vencido aparece como aguardando atualização, sem zerar artificialmente o consumo.
- **Sincronização:** fonte e horário da última consulta na aba Codex; erros continuam visíveis quando há dados anteriores. Sem API disponível, o Codex pode mostrar o último registro local, identificado como histórico.

O uso dos planos é consultado a cada 60 segundos e o histórico local a cada 30 segundos. A API Codex retornando HTTP 429 adia as próximas tentativas por pelo menos cinco minutos, inclusive ao clicar em atualizar. Os últimos dados são preservados com indicação de falha. Uma autenticação expirada ou recusada pede para reabrir o Codex ou executar `codex login`; o monitor relê suas credenciais sem alterá-las.

## Instalação e execução

Requisitos: Windows 10/11, Python 3.10+ e os clientes dos provedores instalados e autenticados.

```powershell
python -m pip install -r requirements.txt
python main.py
```

Também é possível usar `install.bat`, `run.bat` ou `run_hidden.vbs`.

- A janela inicia compacta, com o percentual do provedor selecionado. Passe o mouse para ver limites e horários de reset.
- Clique em **⤢** para expandir e escolha Claude, Codex ou Cursor.
- Clique em **↻** para atualizar. O resultado avisa a interface assim que termina.
- Arraste pelo percentual na janela compacta ou pelo cabeçalho da janela expandida. A posição é preservada durante atualizações e mudanças de modo; a expansão é ajustada para caber na tela.
- Clique em **—** para recolher. Para encerrar, use o menu do ícone na bandeja → Sair.

## Caminhos e persistência

| Dados | Local padrão |
|---|---|
| Claude | `~/.claude/.credentials.json` e `~/.claude/projects` |
| Codex | `~/.codex/auth.json` e `~/.codex/sessions` |
| Cursor | `%APPDATA%/Cursor/auth.json`, banco local e `~/.cursor/projects` |
| Cache Codex e log | `%LOCALAPPDATA%/SimpleLimite/` |

`CODEX_HOME` e `CLAUDE_CONFIG_DIR` permitem usar diretórios personalizados. Esses caminhos independem da pasta do app. O cache Codex é separado por conta, preserva a hora original da consulta e não salva tokens OAuth, e-mail ou IDs de resgate de resets extras. O arquivo antigo `config.json` de calibração não é utilizado pela versão atual, que consulta limites reais.

No Codex, o consumo de hoje é calculado pelas diferenças entre os registros cumulativos das sessões, evitando atribuir ao dia atual todo o consumo de uma conversa iniciada antes. O histórico pode estar incompleto se o cliente não registrar eventos locais.

## Validação

```powershell
python -m unittest discover -s tests -v
```

Os testes cobrem janelas de uso, créditos extras, resets, falhas de conexão, cache por conta, rate limit, consumo entre dias e atualização da interface após arrastar a janela.

## Referência e privacidade

A integração Codex segue os formatos e endpoints documentados no código do [ai-usagebar](https://github.com/akitaonrails/ai-usagebar/tree/main/src/openai): `/backend-api/wham/usage` e `/backend-api/wham/rate-limit-reset-credits`. Esses endpoints dos clientes podem mudar; respostas sem janelas reconhecidas são tratadas como erro e preservam os últimos dados válidos.

As consultas usam as credenciais locais e são enviadas aos respectivos provedores. A aba Claude mostra custos estimados do histórico conforme a tabela em `main.py`; esses valores não representam a cobrança do plano.

## Licença

```
MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
