# Décision de routage et tarifs API — 5 septembre 2026

Cette preuve fige les hypothèses utilisées par la configuration API-first. Les
prix sont en USD par million de tokens, hors TVA, promotions et retries. Le
routeur accepte au plus 4 096 tokens estimés de contexte et réserve au plus 512
tokens de sortie. Pour ne pas sous-compter une tokenisation inconnue, la
réservation d'entrée utilise le nombre d'octets UTF-8 du prompt complet, plus
2 048 tokens de marge protocolaire. Dans le pire cas Unicode autorisé par la
limite de 12 000 caractères, elle est donc de l'ordre de 51 000 tokens, et non
de 5 120. Il trie les providers au coût réservé uniquement à l'intérieur du
rôle de capacité choisi.

La façade Hermes applique une enveloppe distincte adaptée à son protocole :
1 Mio de tokens d'entrée au maximum, 512 tokens de sortie et 4 096 tokens de
marge. Tous ses appels authentifiés partagent le même plafond de mission de
0,05 USD par journée UTC, persisté dans SQLite et donc non réinitialisable par
un redémarrage ou par le champ client `user`. Son identifiant interne utilise
un namespace avec `/`, caractère refusé dans les `mission_id` soumis à
l'API/MCP : une route ordinaire ne peut donc ni fusionner avec ce compteur ni
le saturer. Les plafonds Qwen quotidiens et mensuels restent des bornes
supplémentaires.

## Tarifs retenus

Le tableau suivant compare un appel dont l'estimation conservatrice est de
5 120 tokens d'entrée et 512 de sortie. Il ne remplace pas les paliers complets
encodés dans `orchestrator.json`.

| Provider configuré | Modèle épinglé | Entrée | Sortie | Exemple 5 120 entrée + 512 sortie |
| --- | --- | ---: | ---: | ---: |
| Qwen utility | `qwen3.7-flash-2026-07-15` | 0,028 | 0,110 | 0,000200 USD |
| DeepSeek ops (Alibaba Global) | `deepseek-v4-flash` | 0,138 | 0,275 | 0,00084736 USD |
| Qwen coder | `qwen3-coder-flash-2025-07-28` | 0,144 | 0,574 | 0,001032 USD |
| Qwen ops | `qwen3.7-plus-2026-05-26` | 0,276 | 1,101 | 0,001977 USD |
| DeepSeek ops (API directe) | `deepseek-v4-flash` | 0,440 | 1,320 | 0,002929 USD |
| DeepSeek reasoning/coder | `deepseek-v4-pro` | 1,320 | 3,960 | 0,008786 USD |
| Qwen reasoning | `qwen3.8-max-0902` | 1,650 | 4,951 | 0,010983 USD |

Les paliers Qwen facturés selon le nombre de tokens d'entrée sont tous réservés
par le code :

| Modèle Qwen | Tokens d'entrée | Entrée / M | Sortie / M |
| --- | ---: | ---: | ---: |
| `qwen3.7-flash-2026-07-15` | ≤ 32 000 | 0,028 | 0,110 |
|  | 32 001–256 000 | 0,083 | 0,330 |
|  | 256 001–1 000 000 | 0,165 | 0,660 |
| `qwen3.7-plus-2026-05-26` | ≤ 256 000 | 0,276 | 1,101 |
|  | 256 001–1 000 000 | 0,826 | 3,301 |
| `qwen3-coder-flash-2025-07-28` | ≤ 32 000 | 0,144 | 0,574 |
|  | 32 001–128 000 | 0,216 | 0,861 |
|  | 128 001–256 000 | 0,359 | 1,434 |
|  | 256 001–1 000 000 | 0,717 | 3,584 |

Les tarifs Qwen et DeepSeek hébergé par Alibaba correspondent au scope
Model Studio `Global` et restent identiques sur l'endpoint partagé Singapore
`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`. La borne Unicode pessimiste
d'environ 51 000 tokens place donc Qwen Flash dans son deuxième palier, même si
le contexte logique du routeur est limité à 4 096 tokens. Une estimation
au-dessus du dernier palier est refusée avant egress. Pour l'API DeepSeek
directe, la réservation utilise volontairement les prix de pointe et cache
miss. Les prix hors pointe et cache hit ne réduisent jamais la réservation.

Le provider `alibaba-deepseek-ops-api` est limité à 1 USD par jour, 15 USD par
mois et 0,25 USD par mission. À 5 120 tokens d'entrée et 512 de sortie, son coût
nominal exact est `(5 120 × 0,138 + 512 × 0,275) / 1 000 000`, soit
0,00084736 USD. Il appartient uniquement à `ROLE_LOCAL_OPS`, avant Qwen3.7 Plus
puis l'API DeepSeek directe ; il n'est pas ajouté à `ROLE_CODER`.

Sources officielles : [tarifs Alibaba Model Studio](https://www.alibabacloud.com/help/en/model-studio/model-pricing),
[API Qwen OpenAI-compatible et endpoints régionaux](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions),
[API DeepSeek via Alibaba Model Studio](https://www.alibabacloud.com/help/en/model-studio/deepseek-api),
[fiche DeepSeek V4 Flash Alibaba Model Studio](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/deepseek-v4-flash),
[tarifs et modèles DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/),
[modes thinking DeepSeek](https://api-docs.deepseek.com/guides/thinking_mode/).

Le thinking est désactivé pour ces appels bornés : `enable_thinking=false`
pour le dialecte Alibaba utilisé par Qwen et par DeepSeek hébergé dans Model
Studio, et `thinking.type=disabled` pour l'API DeepSeek directe. Cela évite
qu'un raisonnement caché consomme la petite réserve de sortie avant l'enveloppe
JSON. Le niveau de puissance augmente par changement de modèle, pas par dépense
de tokens de raisonnement non bornée.

## Résidence et alternatives

Le point d'accès partagé Singapore avec scope `Global` ne garantit pas que
l'inférence reste dans l'UE. Le choix strictement UE le plus proche chez Qwen est
`qwen3.5-flash-2026-02-23`, à 0,100/0,400 USD, et nécessite une configuration
ainsi qu'une recette séparées.

Le modèle nominalement moins cher trouvé est Cloudflare Workers AI
`@cf/ibm-granite/granite-4.0-h-micro`, à 0,017/0,112 USD, soit environ 28 % de
moins que Qwen3.7 Flash sur l'exemple de réservation. Il n'est pas moins cher
sur toute charge : sa sortie coûte 0,112 contre 0,110 USD/M chez Qwen. Il n'est
pas retenu à ce stade : Granite annonce bien le function calling, mais la liste
officielle JSON Mode ne le contient pas et Cloudflare ne garantit de toute façon
pas la conformité au schéma. Aucun canari local n'a encore validé ses tours
d'outils, son français ou l'enveloppe stricte de l'orchestrateur. Workers AI
accorde 10 000 Neurons gratuits par jour ; au-delà, le plan Workers payant
commence à 5 USD/mois. L'économie absolue hors quota est inférieure à 0,000056
USD sur cet exemple, alors qu'une sortie invalide déclenche un repli et augmente
coût et latence. Il peut être évalué en shadow/canary, jamais ajouté
automatiquement au chemin de production.

Sources officielles : [prix Workers AI](https://developers.cloudflare.com/workers-ai/platform/pricing/),
[fiche Granite 4 H Micro](https://developers.cloudflare.com/ai/models/%40cf/ibm-granite/granite-4.0-h-micro/),
[function calling Workers AI](https://developers.cloudflare.com/workers-ai/features/function-calling/),
[limites du JSON Mode](https://developers.cloudflare.com/workers-ai/features/json-mode/),
[prix du plan Workers](https://developers.cloudflare.com/workers/platform/pricing/).

Les quotas gratuits ou promotions d'accueil sont exclus : ils sont temporaires,
régionaux ou peuvent refuser brutalement les appels après épuisement. Les prix
et disponibilités doivent être revérifiés avant la migration `/etc`, puis à
chaque annonce fournisseur.
