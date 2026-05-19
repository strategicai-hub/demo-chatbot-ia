# PLANO START - Guia de Criacao de Novo Projeto

## Pre-requisitos

- **gh CLI** instalado e autenticado (`gh auth login`)
  - Instalar: https://cli.github.com/
- **Python 3.12+** instalado
- **Git** instalado

## O que ter em maos antes de comecar

| Item | Onde conseguir |
|------|---------------|
| UAZAPI token | Painel UAZAPI > Instancia > Token |
| UAZAPI instancia | Nome da instancia criada no UAZAPI |
| GEMINI API key | https://aistudio.google.com/apikey |
| Google Sheets credentials JSON | Console GCP > Service Account > Keys |
| Google Sheet ID | URL da planilha (entre /d/ e /edit) |
| Telefone do dono | Numero com DDI (ex: 5511999990000) |
| Dados do negocio | Nome, endereco e demais campos do nicho (ver client.example.yaml) |

---

## Criar novo projeto

```bash
# Na primeira vez, clone o template
gh repo clone gustavocastilho-hub/plano-start-template

# Entre no diretorio
cd plano-start-template

# Execute o setup
python setup.py
```

O script vai perguntar:
1. **Nome do negocio** - ex: "Luitz Prime"
2. **Slug** - identificador unico (ex: "luitz-prime"), gerado automaticamente
3. **Nome da assistente** - ex: "Vic"
4. **Telefone do dono** - para receber alertas
5. **UAZAPI token** - token da instancia WhatsApp
6. **GEMINI API key** - chave da API do Google Gemini
7. **Google Sheet ID** - ID da planilha de leads (pode pular)
8. **Google credentials JSON** - caminho do arquivo (pode pular)

### O que o setup faz automaticamente:

| Passo | Acao | Status |
|-------|------|--------|
| 1 | Cria repositorio no GitHub | Automatico |
| 2 | Substitui placeholders nos deploys | Automatico |
| 3 | Gera `.env` e `client.yaml` | Automatico |
| 4 | Commit e push (dispara build) | Automatico |
| 5 | Configura permissoes do GitHub Actions | Automatico |
| 6 | Aguarda build da imagem Docker | Automatico |
| 7 | Torna pacote GHCR publico | Automatico |
| 8 | Cria stack no Portainer + webhook | Automatico |
| 9 | Salva webhook URL como GitHub secret | Automatico |

---

## Apos o setup

### 1. Preencher client.yaml

Abra o arquivo `client.yaml` no novo repositorio e preencha **todos** os dados do negocio.

#### Secoes do client.yaml

**`niche`** - Nicho do negocio (define o prompt usado)
```yaml
# Nichos disponiveis: capital_de_giro, consorcio, material_construcao,
# petshop, lancamento_livro.
# Tambem detectado dinamicamente pela mensagem inicial do lead.
niche: "capital_de_giro"
```

**`business`** - Dados basicos
```yaml
business:
  name: "Luitz Prime"
  address: "Avenida Higienópolis, nº 1100 - Londrina-PR"
```

**`assistant`** - Nome (greeting e injetado pelo codigo conforme horario de SP)
```yaml
assistant:
  name: "Vic"
  greeting: ""
```

**`capital_de_giro`** - Tipos de bem aceitos como garantia (apenas no nicho capital_de_giro)
```yaml
capital_de_giro:
  asset_types:
    - "Imóvel urbano"
    - "Imóvel rural"
    - "Terreno"
    - "Área rural"
    - "Automóvel"
```

**`payment`** - Formas de pagamento
```yaml
payment:
  methods:
    - "crédito"
    - "PIX"
    - "boleto"
```

**`differentials`** - Diferenciais do negocio
```yaml
differentials:
  - "Atendimento Humanizado"
  - "Consultoria Total e Ativa"
  - "Plano de Pagamento Personalizado"
```

**`media`** - Midias (imagens, videos)
```yaml
media:
  "[IMAGEM_EXEMPLO]":
    url: "https://exemplo.com/foto.jpg"
    type: "image"
```

Apos preencher, faca o push:
```bash
cd {slug}
git add client.yaml
git commit -m "feat: dados do negocio preenchidos"
git push
```

### 2. Configurar webhook na UAZAPI

No painel UAZAPI, configure o webhook da instancia para:
```
https://webhook-whatsapp.strategicai.com.br/{slug}
```

> Este e o **unico passo manual** necessario.

### 3. Testar

1. Envie uma mensagem para o numero WhatsApp da instancia
2. Acesse o painel: `https://webhook-whatsapp.strategicai.com.br/{slug}/painel`
3. Verifique se a mensagem apareceu e se o bot respondeu

---

## Atualizando a partir do template

Quando fizer melhorias no template e quiser aplicar nos projetos existentes:

```bash
# No diretorio do projeto do cliente
cd {slug}

# Adicionar o template como remote (so precisa fazer 1 vez)
git remote add template https://github.com/gustavocastilho-hub/plano-start-template.git

# Buscar atualizacoes
git fetch template

# Fazer merge das atualizacoes
git merge template/main --allow-unrelated-histories

# Resolver conflitos se houver, depois push
git push
```

---

## Ajustando o template

Para fazer melhorias que valem para todos os futuros projetos:

```bash
cd plano-start-template

# Faca suas alteracoes
git add -A
git commit -m "feat: descricao da melhoria"
git push
```

---

## Resumo do fluxo

```
python setup.py
    |
    v
[Tudo automatico: repo, build, Portainer, secrets]
    |
    v
Preencher client.yaml + push
    |
    v
Configurar webhook UAZAPI (unico passo manual)
    |
    v
PRONTO! Bot funcionando.
```
