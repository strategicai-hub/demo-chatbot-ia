"""
Carrega os dados do negocio a partir de um arquivo client*.yaml.

- `load_client_data()` (sem argumento) retorna o conteudo de `client.yaml` — usado
  como base / fallback.
- `load_client_data(niche="petshop")` tenta `client.petshop.yaml` primeiro e cai
  em `client.yaml` se nao existir. Permite manter dados de varios nichos no mesmo
  repositorio (essencial para o demo multi-nicho).
"""
from functools import lru_cache
from pathlib import Path

import yaml

_BASE_DIR = Path(__file__).parent.parent


@lru_cache
def load_client_data(niche: str | None = None) -> dict:
    candidates = []
    if niche:
        candidates.append(_BASE_DIR / f"client.{niche}.yaml")
    candidates.append(_BASE_DIR / "client.yaml")

    for path in candidates:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}

    raise FileNotFoundError(
        f"Nenhum arquivo de cliente encontrado. Tentativas: "
        f"{', '.join(str(p) for p in candidates)}. "
        "Copie client.example.yaml para client.yaml e preencha os dados."
    )
