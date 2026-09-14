# Claude Code — mode Ops supervisé

Claude Code 2.1.236 est installé depuis le RPM officiel et le dépôt signé
d'Anthropic. Sa configuration MCP est administrée au niveau système par
`/etc/claude-code/managed-mcp.json`, conformément au mécanisme Linux officiel.

Les trois serveurs locaux sont :

- `ops-broker` : frontière de toutes les missions, actions et approbations ;
- `ops-orchestrator` : routage borné et lecture des budgets/traces ;
- `ops-memory` : mémoire cloisonnée par rôle, projet, environnement et niveau.

Les handshakes réels annoncent respectivement 31, 5 et 7 outils, soit 43 outils
avant application des frontières d'identité et allowlists propres à la requête.

Claude utilise l'identité fixe `claude-supervised`, distincte de
`codex-supervised`. Le broker reste deny-by-default et les appels d'outils
restent soumis aux confirmations interactives de Claude Code. Aucun secret ne
figure dans la configuration MCP.

Vérifications locales :

```bash
claude doctor
claude mcp list
sudo -u ops-user /opt/ops-memory/mcp-venv/bin/python \
  /home/ops-user/ops-control-plane/scripts/verify-mcp-live.py --profile claude
```

L'authentification au compte Claude est volontairement une étape humaine :

```bash
claude auth login
```

Après authentification, lancer Claude depuis le dépôt Ops afin qu'il charge
également `/home/ops-user/.claude/CLAUDE.md` et les consignes propres au dépôt.
