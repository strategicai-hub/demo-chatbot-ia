# Instruções para o assistente de IA

## Estrutura do projeto

Este repositório (`plano-start-template`) é o **template base** do qual todos os projetos de clientes são derivados.

Cada cliente tem seu próprio repositório separado (ex: `gracie-barra`), criado a partir deste template.

## Regra principal: sincronização cliente → template

Sempre que fizer uma correção ou melhoria em um projeto de cliente, avaliar se a mudança é **genérica** (não depende de dados específicos do cliente) e, se for, aplicar a mesma correção neste template também.

### Como identificar se vai pro template

| Tipo de mudança | Vai pro template? |
|---|---|
| Correção de bug em `app/*.py` | Sim |
| Melhoria de regra no `prompt_template.j2` | Sim |
| Remoção de conteúdo hardcoded de outro cliente | Sim |
| Novo campo genérico no `client.example.yaml` | Sim |
| Dados específicos do cliente (preços, horários, endereço) | Não |
| Conteúdo de modalidade específica (ex: Jiu-Jitsu Kids) | Não |

### Fluxo

1. Corrigir no projeto do cliente
2. Avaliar se é genérico
3. Se sim: aplicar o mesmo no template
4. Commitar os dois repositórios separadamente

## Projetos derivados diretos

### Templates derivados

- `plano-pleno-template` — Template estendido com follow-ups e integrações
  - Repo: https://github.com/strategicai-hub/plano-pleno-template

### Clientes derivados

- `aje-de-boxe` — Academia de boxe (AJE)
  - Repo: https://github.com/strategicai-hub/aje-de-boxe

> **Importante:** estas listas são a **fonte de verdade** usada por `scripts/sync-to-derived.sh`. Ao adicionar um novo derivado, inclua o link do repo aqui.
>
> **Propagação em cascata.** O `plano-pleno-template` é derivado deste template e recebe commits via cherry-pick aqui. Os **clientes do plano-pleno** ficam no CLAUDE.md do próprio `plano-pleno-template` (sync em cascata: `start → pleno → clientes do pleno`).

## Sincronização template → projetos derivados

Para aplicar um commit genérico deste template em todos os projetos derivados listados acima:

```bash
./scripts/sync-to-derived.sh <commit-sha>
```

O script:
1. Lê a lista de repos derivados desta seção do CLAUDE.md
2. Clona cada um, faz `git cherry-pick -x <commit-sha>` e `git push`
3. Reporta sucessos e falhas ao final

Conflitos de cherry-pick são reportados e o repo é deixado limpo (cherry-pick abortado) — resolva manualmente nesses casos.
