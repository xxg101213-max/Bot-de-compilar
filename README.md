# Compilador de source code via Telegram (iOS)

Bot privado em **Python + Railway** que aceita `.zip` no Telegram, envia o código para uma **GitHub Release temporária**, dispara uma **GitHub Action num macOS isolado**, recolhe o arquivo compilado e responde com `.dylib` (Theos) ou `.ipa` sem assinatura (Xcode).

## Requisitos

- Conta no Telegram e bot criado em [@BotFather](https://t.me/BotFather).
- Repositório GitHub **privado** com estes arquivos enviados **na raiz**, incluindo `.github/workflows/build.yml`.
- GitHub Actions habilitado, com runner `macos-26` disponível.
- Token fine-grained do GitHub com acesso **somente a esse repositório** e permissões de **Contents: Read and write** e **Actions: Read and write**.
- Conta Railway ou outro host Docker sempre ativo. Railway pode ter custos; GitHub Actions no macOS também pode consumir minutos faturáveis.

## Como instalar

1. Extraia `telegram-source-compiler.zip` e envie os arquivos para um novo repositório GitHub privado; não envie a `.env` preenchida.
2. Em `https://t.me/BotFather`, crie um bot via `/newbot` e copie o token.
3. No GitHub, abra **Settings > Developer settings > Personal access tokens > Fine-grained tokens**, crie um token restringido ao repositório privado e configure as duas permissões acima.
4. No Railway, crie um novo projeto a partir desse repositório. O Dockerfile na raiz instala e inicia o bot automaticamente.
5. Na área de **Variables** do Railway, insira:

   ```env
   TELEGRAM_BOT_TOKEN=token_criado_no_botfather
   GITHUB_TOKEN=github_pat_seu_token
   GITHUB_REPO=seu_usuario/repositorio_privado
   GITHUB_BRANCH=main
   ALLOWED_USER_IDS=123456789
   ```

   Para descobrir seu ID de usuário Telegram, consulte o seu ID pelo Telegram. Se iniciar o bot com um ID permitido provisório diferente do seu, o `/start` responderá informando o ID real, que você deve cadastrar em `ALLOWED_USER_IDS` e aplicar as novas variáveis no Railway.
6. Faça o deploy; envie `/start` ao bot. Ele responderá com orientações.
7. Envie um `.zip` com o projeto inteiro e aguarde a resposta. O bot processa apenas usuários da lista `ALLOWED_USER_IDS`.

## Comandos do Telegram

- `/start` ou `/help`: instruções.
- `/modo auto`: detecta Xcode (`.xcodeproj`) e usa IPA, caso contrário tenta Theos.
- `/modo ipa`: compila aplicativo iOS usando Xcode e entrega IPA **não assinado**.
- `/modo dylib`: compila um projeto de tweak Theos e entrega a dylib.
- `/status`: informa o ID da compilação em andamento.

## Estrutura necessária dos arquivos enviados

**Para dylib:** `.zip` de um projeto **Theos válido**, contendo `Makefile` (com referência ao `THEOS`) e arquivos `.m`, `.mm`, etc. O `Makefile` deve ter as dependências adequadas e produzir `.dylib`. Um arquivo `.m` solto não é suficiente para esse pipeline.

**Para IPA:** `.zip` com `.xcodeproj` ou `.xcworkspace`, com esquema Xcode **compartilhado**, código Swift/Objective-C, assets e dependências. O pipeline seleciona automaticamente o primeiro esquema disponível que não seja de testes. Em projetos com vários apps/esquemas, altere `scripts/build_project.py` para selecionar o esquema correto.

**Atenção:** o `.ipa` compilado sai **sem assinatura**. Para instalar em iPhone normal será preciso assinar por um método legítimo (certificado/provisioning profile, Xcode ou outro serviço de assinatura autorizado). Projetos com bibliotecas, SDKs privados ou dependências não presentes no runner podem exigir configuração extra. Nem todos os sources são compiláveis de forma automática.

## Segurança e limites

- **Somente seu ID** pode enviar tarefas. Nunca compartilhe `TELEGRAM_BOT_TOKEN` ou `GITHUB_TOKEN`.
- Código recebido **não** roda no container do bot. A compilação roda em um runner efêmero macOS do GitHub com permissão de repositório **somente leitura**.
- ZIP enviado: até 18 MB; até 2.000 arquivos e 400 MB descompactados.
- Arquivo enviado de volta: até 49 MB. Se exceder, consulte os artefatos do workflow no GitHub.
- As releases de origem são removidas após a tarefa. Artefatos de saída são retidos por 2 dias.
- Se o Railway for reiniciado durante uma tarefa, o acompanhamento dessa tarefa pode ser interrompido; o workflow poderá continuar no GitHub.
- **Não abra acesso público** a esse bot sem antes implementar filas, rate limit, quota e proteção contra builds abusivos. Compilar código enviado por terceiros envolve risco, mesmo com runners isolados.
- Recomenda-se manter os workflow files e o bot em um repositório privado, usar um PAT com escopo mínimo e não armazenar certificados/chaves de assinatura no runner de builds de código recebido.

## O que acontece em caso de falha?

O bot fornece um link para as **GitHub Actions** do repositório. Em geral, você encontrará mensagens como:

- `Makefile de projeto Theos não encontrado` → enviar projeto Theos completo.
- `Nenhum scheme compartilhado` → definir shared scheme no Xcode.
- `Xcode não produziu aplicativo` → o projeto pode ser uma biblioteca/framework, não um app.
- `Signing...` ou `SDK missing` → depende da configuração do projeto e SDKs.

## Arquivos

- `bot.py` — integração Telegram/GitHub.
- `.github/workflows/build.yml` — runner macOS.
- `scripts/build_project.py` — detecção e compilação.
- `Dockerfile`, `requirements.txt` — deploy no Railway.
- `.env.example` — variáveis necessárias.
