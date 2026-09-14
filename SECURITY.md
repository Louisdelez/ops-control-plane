# Sécurité

Le projet est en développement et comprend des composants privilégiés. Aucune branche de maintenance ni délai de correction garanti n’est annoncé.

## Signalement

Si disponible, utiliser **Security → Report a vulnerability** sur GitHub. Sinon, ouvrir une issue demandant un contact privé sans exposer les détails d’exploitation. Fournir une reproduction avec des données synthétiques ; ne jamais joindre de vrai secret ou de sauvegarde.

## Protection

Les secrets sont gérés par OpenBao. Les opérations passent par les permissions et runbooks du broker. Les souvenirs n’accordent aucun droit. Les pages distantes ne doivent pas accéder aux commandes Tauri privilégiées. Une reprise ambiguë nécessite une revue.

Le `.gitignore` exclut les formats et emplacements sensibles courants, mais ne constitue pas un détecteur de secrets. Contrôler les fichiers staged et scanner le commit avant publication. Si un secret a été publié, le révoquer : supprimer un fichier du dernier commit ne l’efface pas de l’historique.

Les configurations sont des exemples à revoir pour chaque installation, notamment droits Unix, réseau, TLS, origines web, budgets et récupération.

## Contrôler le commit préparé

Installer Gitleaks, puis vérifier l’arbre exact de l’index Git :

```sh
git add --all
python3 scripts/check-publication.py
```

Le contrôle refuse les chemins sensibles et lance Gitleaks sur une copie temporaire des fichiers staged. Les exceptions de `.gitleaks.toml` sont limitées à des valeurs synthétiques de tests et deux affectations JavaScript tierces vérifiées. Toute nouvelle exception doit être justifiée.
