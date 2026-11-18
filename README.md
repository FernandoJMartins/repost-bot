# repost-bot — backup/espelhamento de chats do Telegram

Bot em Python que copia automaticamente as mensagens de um chat de origem
(canal, grupo ou supergrupo) para um chat de destino, mantendo mídia, legendas,
ordem e álbuns, com fila persistente em SQLite, idempotência e recuperação
após quedas.

> Uso pessoal, apenas em chats cujo conteúdo você tem autorização para copiar.

---

## 1. Qual API e por quê

Documentação consultada em 27/10/2026: [Bot API](https://core.telegram.org/bots/api)
(versão 10.3), [Bots FAQ](https://core.telegram.org/bots/faq) e
[MTProto – updates](https://core.telegram.org/api/updates).

| | **Bot API** (escolhida) | **Telegram API / MTProto** (conta de usuário) |
|---|---|---|
| Autenticação | token do @BotFather | `api_id` + `api_hash` + login com telefone; gera uma sessão que dá acesso total à sua conta |
| Histórico anterior | Não há método para ler histórico. Só é possível copiar por ID (`copyMessage(s)`) sem saber o conteúdo antes | `messages.getHistory`, `channels.getMessages` (até 200 IDs por chamada) |
| Atualizações perdidas | `getUpdates` guarda updates não confirmados por **no máximo 24 h** | `updates.getDifference` / `getChannelDifference`; limitado ao tamanho da “caixa” do canal (~100 mil eventos), depois retorna `channelDifferenceTooLong` |
| Cópia sem baixar | `copyMessage` / `copyMessages` (1 a 100 IDs, mantém álbuns) | `messages.forwardMessages` com `drop_author` |
| Grupos | Precisa ser admin **ou** ter o modo de privacidade desativado; nunca vê mensagens de outros bots | Vê tudo o que a sua conta vê |
| Limites | ~1 msg/s por chat, ≤ 20 msg/min em grupos, ~30 msg/s no total | `FLOOD_WAIT_X` dinâmicos e não publicados |
| Risco | Baixo: pior caso, o bot é limitado/banido | Maior: automação de conta de usuário pode levar a restrição/ban **da sua conta pessoal**; sessão vazada = conta comprometida |
| Conteúdo protegido | Bloqueado | Bloqueado também (as restrições valem para ambos) |

**Decisão:** Bot API. É a opção mais simples, segura e suportada para
espelhamento contínuo: `copyMessage(s)` copia mídia no servidor do Telegram
(nada é baixado/reenviado), mantém legendas e álbuns, e não arrisca sua conta
pessoal. MTProto só se justifica se você precisar de **histórico completo de
grupos básicos** ou de **conteúdo de outros bots em grupos**, aceitando o risco
para a conta (não implementado aqui de propósito).

## 2. Como funciona

```
getUpdates (long polling) ──► SQLite (fila + offset na MESMA transação) ──► worker ──► copyMessage(s) ──► destino
                                    ▲                                         │
                                    └──── status: pending/sending/done/... ◄──┘
```

- **Sem duplicidade:** cada mensagem de origem tem uma linha única
  `(source_chat_id, source_message_id)`. O offset do `getUpdates` só avança no
  banco junto com a gravação da fila; o Telegram só descarta o update na
  próxima chamada. Se o bot cair antes, o update é reentregue e o `UNIQUE` o
  ignora.
- **Janela de incerteza:** antes de chamar a API a linha vira `sending`. Se o
  processo morrer ou a conexão cair depois que a requisição saiu (timeout de
  leitura, HTTP 500/504), não há como saber se a cópia chegou — a Bot API não
  lê o histórico do destino. Com `UNCERTAIN_POLICY=hold` (padrão) essas
  mensagens ficam como `uncertain` e **nunca** são reenviadas sozinhas; você
  confere o destino e usa `/resend` ou `/skip`. Erros em que a conexão nem
  abriu e respostas 429 são repetidos com segurança.
- **Ordem:** o worker sempre processa a menor `message_id` pendente; uma
  mensagem com erro temporário segura as seguintes até ser enviada ou
  esgotar `MAX_ATTEMPTS` (backoff exponencial até 15 min).
- **Álbuns:** as partes chegam em updates separados com o mesmo
  `media_group_id`; o bot espera `ALBUM_SETTLE_SECONDS` e copia tudo com um
  único `copyMessages`, que mantém o agrupamento.
- **Limites:** limitador de `DEST_MESSAGES_PER_MINUTE` (padrão 20, nunca mais
  que 1/s); cada item de um álbum conta como uma mensagem. Em HTTP 429 o bot
  respeita `retry_after` + 1 s e não conta como tentativa.
- **Reconexão:** backoff exponencial com jitter (até 5 min) no polling.
- **Recuperação de lacunas (canal/supergrupo):** nesses chats o `message_id`
  é sequencial. Se chega a mensagem 150 e a última vista era 140, os IDs
  141–149 são enfileirados e, após `GAP_SETTLE_SECONDS`, copiados em lotes
  consecutivos com `copyMessages` (IDs apagados são pulados pela API). Isso
  recupera mensagens perdidas por quedas maiores que 24 h, mensagens de outros
  bots em supergrupos e mensagens não entregues por privacidade.
- **Erros de configuração** (bot removido, sem permissão, chat migrado):
  o backup é pausado automaticamente, você é avisado no privado e usa
  `/resume` depois de corrigir.

### Tabela `messages`

`source_message_id`, `media_group_id`, `kind` (photo, video, text…),
`file_unique_id`, `origin` (update/gap/backfill), `status`
(pending/sending/done/skipped/failed/uncertain), `dest_message_id`
(ou `dest_message_ids` em lotes sem mapeamento 1:1), `attempts`,
`last_error`, `source_date`, `created_at`, `updated_at`, `copied_at`.

## 3. Página web de configuração

Tudo é feito pela página web, sem editar variáveis de ambiente:

1. Inicie o bot (seção 4) e abra **http://localhost:8080**.
2. **Login:** usuário `admin`, senha `admin`. A página mostra um aviso até você
   trocar a senha na seção *Conta* (ou defina `WEB_USER`/`WEB_PASSWORD` no `.env`
   antes do primeiro início).
3. **Token:** crie um bot dedicado no [@BotFather](https://t.me/BotFather), cole o
   token e clique em *Salvar e testar*. O token fica só no banco local e a página
   mostra apenas uma versão mascarada.
4. **Grupos e canais:** use os botões *+ Adicionar bot a um canal* / *a um grupo*
   (links oficiais `t.me/<bot>?startchannel` / `?startgroup`). Assim que o
   Telegram avisa que o bot entrou, o chat aparece na lista. Depois é só clicar
   em **Origem** e **Destino**: nada de digitar `-100…`.
5. **Administrador (opcional):** clique em *Abrir conversa com o bot*, envie
   `/start` e clique em **Sou eu**. Com isso você recebe avisos no Telegram e
   pode usar os comandos da seção 5.

### Como a lista de chats é preenchida

**A Bot API não tem um método para listar os chats em que o bot está.** A lista
é montada a partir do que o Telegram envia ao bot:

| Evento | Aparece na lista? |
|---|---|
| Bot adicionado, promovido, rebaixado ou removido (`my_chat_member`) | sim, com o status e a permissão de postar |
| Qualquer post no canal / mensagem visível no grupo | sim |
| Você encaminha uma mensagem do canal para o bot, no privado | sim |
| Você digita `@canal_publico`, link `t.me/...` ou o ID em *Adicionar* | sim (via `getChat`) |
| Bot já estava num chat *antes* desta versão e não há atividade | não: remova e adicione o bot de novo, ou poste algo |

*Atualizar lista* consulta de novo cada chat conhecido (`getChat` +
`getChatMember`) para mostrar se o bot continua lá, se é admin e se pode postar.
Ao trocar origem, destino ou ajustes, o bot reinicia sozinho: ele espera a
cópia em andamento terminar, então a troca não gera duplicidade.

### Permissões necessárias

- **Origem canal:** bot administrador (nenhuma permissão extra para ler).
- **Origem grupo/supergrupo:** bot administrador *ou* modo de privacidade
  desativado (`/setprivacy` → Disable no BotFather; depois remova e adicione o bot).
- **Destino canal:** bot administrador com *Publicar mensagens*.
- **Destino grupo:** bot membro com permissão de enviar mídia.

A página avisa quando alguma dessas condições não é atendida.

## 4. Instalação e execução

### Local (Linux/macOS/Windows)

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m repost_bot
```

Abra http://localhost:8080. O `.env` é opcional (veja `.env.example`).

### Docker

```bash
mkdir -p data logs && sudo chown 10001:10001 data logs
docker compose up -d --build
# abra http://localhost:8080 e entre com admin / admin
```

A porta é publicada só em `127.0.0.1:8080` do host. O container reinicia sozinho
e o banco (com token e configurações) fica em `./data`.

### systemd

```bash
sudo useradd --system --home /opt/repost-bot repostbot
sudo mkdir -p /opt/repost-bot && sudo cp -r repost_bot requirements.txt /opt/repost-bot/
cd /opt/repost-bot
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo mkdir -p data logs && sudo chown -R repostbot:repostbot /opt/repost-bot
sudo cp /caminho/do/projeto/deploy/repost-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now repost-bot
journalctl -u repost-bot -f      # página: http://localhost:8080 (admin / admin)
```

### Acesso remoto (servidor/VPS)

A página escuta só em `127.0.0.1`. Do seu computador, abra um túnel SSH e
acesse http://localhost:8080:

```bash
ssh -L 8080:127.0.0.1:8080 usuario@seu-servidor
```

Não troque `WEB_HOST` para `0.0.0.0` em servidor exposto sem um proxy com HTTPS na frente.

**Rode apenas uma instância por token** (duas instâncias geram erro 409 e disputam updates).

## 5. Comandos

Pela página: pausar/retomar, estatísticas, fila de falhas/incertas/pendentes
(reenviar ou ignorar), backfill, ajustes avançados, log ao vivo e troca de senha.

No Telegram (somente no privado, somente o admin escolhido):

| Comando | Ação |
|---|---|
| `/status` | ativo/pausado, conexão, fila, falhas, última cópia |
| `/stats` | totais por status e por tipo, cópias nas últimas 24 h |
| `/pause` / `/resume` | pausa/retoma as cópias (a fila continua sendo gravada) |
| `/failed` / `/retry <id\|all>` | lista e reenfileira falhas definitivas |
| `/uncertain` / `/resend <id\|all>` | lista e reenvia entregas incertas (confira o destino antes!) |
| `/skip <id>` | descarta uma mensagem pendente/falha/incerta |
| `/backfill <de> <até>` | copia um intervalo de IDs antigos (canal/supergrupo) |

Comandos de qualquer outro usuário ou de grupos são ignorados e registrados em log.

## 6. Histórico antigo

### Grupos com tópicos (fórum) — recomendado

1. No painel, passo **4 – Sua conta do Telegram**: informe `api_id` e `api_hash`
   (crie em [my.telegram.org/apps](https://my.telegram.org/apps)) e o telefone,
   clique em *Enviar código*, digite o código (e a senha de 2 etapas, se houver).
2. Em **Tópicos**, clique em *Ler tópicos* e escolha para cada tópico da origem:
   *Criar automaticamente* (mesmo nome e cor), *Tópico Geral* ou um tópico que
   você já criou no destino.
3. Clique em **Importar histórico**. A conta lê todas as mensagens (com o tópico
   de cada uma) e o bot copia na ordem, a 20 msg/min (≈ 3 h para 3.700 mensagens).

A conta de usuário **só lê** (nada é enviado por ela). A sessão salva no banco dá
acesso à conta: use *Desconectar conta* quando terminar se não quiser mantê-la.
Mensagens novas não precisam da conta: o bot recebe o tópico de cada uma e usa o
mesmo mapeamento.

### Sem conta de usuário (só o bot)

- Um bot **não** recebe nada anterior à sua entrada no chat, e a Bot API não tem
  método de leitura de histórico.
- Updates não confirmados ficam no servidor por **até 24 h**; dentro desse prazo
  o bot recupera tudo ao voltar.
- Para canal/supergrupo, `/backfill 1 500` tenta copiar os IDs 1–500 com
  `copyMessages`. Isso usa apenas métodos oficiais e respeita o limitador, mas:
  - você não sabe de antemão o que cada ID contém (o conteúdo é copiado “às cegas”);
  - IDs apagados são pulados; mensagens de serviço não são copiáveis;
  - lotes podem dividir um álbum que cruze a fronteira de 100 IDs;
  - o acesso a mensagens anteriores à entrada do bot depende do Telegram
    (normalmente funciona em canais; teste com um intervalo pequeno);
  - a 20 msg/min, 5 000 mensagens levam ~4 h.
- Para histórico de **grupo básico** ou acesso garantido a tudo, só uma conta de
  usuário via MTProto (`messages.getHistory`), com os riscos descritos acima.

## 7. Limitações reais

- **Edições e exclusões não são espelhadas** (edições são registradas em log).
- **Conteúdo protegido** (“Restringir salvamento de conteúdo”) não pode ser copiado.
- Não copiáveis pela API: mensagens de serviço, paid media, giveaways, invoices,
  e quizzes cuja resposta o bot não conhece. Ficam como `skipped` com o motivo.
- Mensagens copiadas **não** mostram “Encaminhado de” nem o autor original,
  data original, reações, views, comentários ou threads de resposta entre mensagens.
- Em grupos de origem, mensagens **de outros bots** não chegam via update; em
  supergrupos elas são recuperadas pela detecção de lacunas, em grupos básicos não.
- Fóruns (tópicos): no destino tudo vai para o tópico geral.
- Lacunas só são detectadas quando chega uma mensagem nova; IDs maiores que
  `GAP_AUTO_MAX` exigem `/backfill` manual.
- Lotes de lacuna em que alguns IDs não existiam não têm mapeamento 1:1
  origem→destino (a lista de IDs de destino fica em `dest_message_ids`).
- Se o grupo de origem virar supergrupo, o ID muda: o bot pausa e você precisa
  atualizar `SOURCE_CHAT_ID`.
- Entregas incertas exigem decisão manual (é o preço de nunca duplicar).
- Não há garantia contra limitações ou banimento por parte do Telegram; o bot
  apenas segue os limites e regras publicados.

## 8. Segurança

- **O token fica no banco `data/repost.db`**: proteja essa pasta (`chmod 700 data`)
  e os backups dela como se fossem a senha do bot. Ela já está no `.gitignore`.
- Página web: só em `127.0.0.1` por padrão; login padrão `admin`/`admin` (troque
  na seção *Conta*, principalmente se for acessar por túnel/proxy); senha guardada
  com PBKDF2 (390 mil iterações); sessões de 7 dias em cookie
  `HttpOnly`/`SameSite=Strict`; POSTs exigem cabeçalho anti-CSRF; bloqueio
  progressivo após 5 senhas erradas; CSP restritiva; o token nunca volta para a página.
- O token é removido de todos os logs (inclusive o log exibido na página).
- Container roda como usuário sem privilégios; unit systemd com `ProtectSystem=strict`.
- Se o token vazar: `/revoke` no @BotFather e cole o novo token na página.

## 9. Testes

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Os testes cobrem classificação, idempotência do banco, lacunas, álbuns,
limitador, tratamento de 429/erros de rede, recuperação após queda,
autorização dos comandos, configuração no banco, descoberta de chats e o fluxo
completo da página web (login, seleção de origem/destino, pausa, backfill) —
sem acessar a rede.
