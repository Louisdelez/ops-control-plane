# Ajouter les clés de mémoire à la fin

Sans clé, SQLite et Qdrant restent utilisables avec la recherche déterministe.
Les terminaux Codex et Claude utilisent leurs comptes natifs indépendamment.
Aucun modèle local n’est installé.

Dans l’onglet **OpenBao**, ouvrir le moteur KV `kv-infra-shared`, puis créer
le secret `llm/siliconflow` avec le champ `api_key` contenant la clé SiliconFlow.
La saisie et le stockage restent dans l’interface native du coffre. Ne pas
coller cette valeur dans un terminal d’assistant, une mission ou un rapport.

Le service `ops-memory-api-secrets` lit uniquement cette référence avec son
AppRole dédié. Il révoque son jeton après lecture, chiffre les deux identifiants
systemd nommés et active automatiquement embedding et reranking en une minute
environ. Une rotation au même emplacement est reprise automatiquement. Une clé
absente ou une lecture du coffre indisponible désactive ce chemin API ; les
identifiants chiffrés précédents ne sont pas utilisés comme preuve de disponibilité.

Modèles préparés : `Qwen/Qwen3-Embedding-0.6B` (1024 dimensions) et
`Qwen/Qwen3-Reranker-0.6B`, via les endpoints API SiliconFlow. Chaque opération
reste soumise au budget mémoire configuré. Ajouter une clé ne prouve ni le crédit
fournisseur ni la qualité des résultats.

Les documents existants ne sont pas envoyés au fournisseur lors de la saisie.
La conversion de souvenirs sélectionnés utilise `memory_reindex_api`, de 1 à
20 identifiants autorisés par appel, dans une collection séparée. Elle nécessite
une recette avec la vraie clé ; celle-ci reste volontairement différée.
