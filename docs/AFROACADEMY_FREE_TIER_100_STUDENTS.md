# AFROACADEMY — plan de capacité gratuite pour 1 000 à 3 000 élèves

## Décision de conception

Objectif : absorber des pics de 1 000 à 3 000 élèves connectés et accepter leurs exercices, conserver chaque demande, la traiter avec une capacité contrôlée et restituer la réponse au bon élève. Le système ne doit jamais annoncer une réponse terminée avant qu'elle soit enregistrée.

**Contrainte non négociable : budget d'infrastructure = 0 USD/mois.** Cette contrainte permet de préparer une file d'attente durable et une meilleure gestion de concurrence, mais ne permet pas de garantir 1 000 à 3 000 générations IA simultanées : les quotas gratuits des fournisseurs restent externes, variables et partagés. En cas de saturation, les demandes doivent rester en attente et être reprises, pas être perdues.

## État vérifié de l'infrastructure

- Supabase projet `afroacademy`, région `eu-west-1`, état `ACTIVE_HEALTHY`.
- Supabase Free annonce 500 MB de base, 5 GB d'egress, 1 GB de Storage, 500 000 invocations Edge Functions et 200 connexions Realtime de pointe. Le plan gratuit peut mettre en pause les projets inactifs.
- Deux Edge Functions existent déjà : `create-test-users` et `list-test-users`. Ne pas les remplacer.
- Render `afroacademy-ai-aggregator` est sur le plan Free, une seule instance, sans mise à l'échelle au-delà d'une instance. Le service s'endort après 15 minutes sans trafic, et son réveil prend environ une minute. Son système de quotas est actuellement stocké dans un fichier JSON local, donc non partagé entre plusieurs instances.
- L'agrégateur fait une cascade séquentielle de fournisseurs. Les compteurs de quota JSON et le délai de cascade actuels ne constituent pas une file de travail multi-instance.
- Le frontend AFROACADEMY dispose déjà de son circuit textuel Kofi et d'un circuit séparé pour les images/PDF. Le circuit des pièces jointes doit rester distinct.

## Architecture cible à coût nul

1. **Vercel reste la porte d'entrée** : authentifier l'élève, vérifier les limites de taille, créer une demande idempotente et renvoyer immédiatement un identifiant de tâche.
2. **Supabase Postgres devient la source de vérité** : table `kofi_jobs` avec propriétaire, conversation, contenu, type de tâche, statut, nombre d'essais, date de prochaine tentative, bail/lease, réponse et code d'erreur public. RLS interdit à un élève de lire les tâches d'un autre.
3. **Réservation atomique des tâches** : fonction SQL courte basée sur `FOR UPDATE SKIP LOCKED`, avec bail expirant, pour éviter que deux travailleurs traitent le même exercice.
4. **Travailleurs bornés** : Edge Function dédiée, appelée à l'arrivée d'une tâche et relancée pour les tâches en attente. Le nombre de traitements simultanés est borné par les quotas réels des fournisseurs, pas par le nombre de visiteurs.
5. **Quota partagé** : table Postgres de fenêtres RPM/TPM/RPD par fournisseur et modèle. Réservation atomique avant appel externe, enregistrement du résultat après l'appel. Ne plus utiliser un fichier local JSON comme source de vérité.
6. **Reprise automatique** : erreurs 429/5xx/timeout -> backoff exponentiel avec jitter, changement de fournisseur quand le quota le permet, puis tâche en attente. Erreur définitive -> statut explicite et exercice conservé.
7. **Retour à l'élève** : statut `queued / processing / completed / retrying / failed`, actualisé par Realtime ou polling léger. La réponse est persistée avant de signaler `completed`.
8. **Protection des pièces jointes** : ne pas envoyer les photos/PDF vers le endpoint texte. Leur pipeline existant reste séparé et ses métadonnées sont conservées dans la tâche.

## Politique de service pour 1 000 à 3 000 élèves

- Concevoir la réception pour 3 000 soumissions en rafale, avec essais progressifs 100 / 500 / 1 000 / 2 000 / 3 000. La capacité de réception n'est pas la capacité d'inférence IA.
- Stocker les images/PDF dans Supabase Storage et seulement leurs références sécurisées dans la file ; imposer des limites de taille et de conservation.
- Ajouter identifiants idempotents, plafonds de tâches par compte et protection anti-abus.
- Traiter en parallèle uniquement jusqu'au budget de concurrence et de débit réellement autorisé par les fournisseurs ; garder le reste en file durable.
- Router chaque demande vers un fournisseur éligible, puis basculer seulement en cas d'échec/quota. Ne pas diffuser chaque exercice à tous les fournisseurs.
- Créer un registre par fournisseur/modèle : RPM, TPM, RPD, contexte maximal, vision, région, conditions de données, date de vérification et état de santé.
- Prévoir des adaptateurs indépendants pour Gemini API, Groq, Cloudflare Workers AI et tout fournisseur dont le quota est réellement confirmé dans le compte AFROACADEMY.
- Un deuxième agrégateur ne multiplie pas les quotas des API amont. Si deux agrégateurs appellent les mêmes fournisseurs, leurs limites restent les mêmes : centraliser les compteurs et éviter les doublons.
- Les autres demandes restent dans une file visible et durable, avec leur position approximative et un état de progression.
- Ne jamais promettre 100 réponses instantanées ou un délai fixe avec des API gratuites.
- Si tous les fournisseurs gratuits sont en panne ou à quota, garder les exercices en attente et afficher une explication honnête.
- Prévoir des limites anti-abus par compte pour empêcher un élève de monopoliser les quotas partagés.

## Phases d'implémentation et critères de validation

### Phase A — fondation persistante
- Ajouter migrations SQL non destructives pour `kofi_jobs`, `kofi_provider_windows` et les fonctions SQL de claim/complete/retry.
- Ajouter tests RLS : élève A ne peut ni lire ni modifier la tâche de B.
- Ajouter tests de concurrence : 3 000 claims simultanés ne donnent jamais deux travailleurs pour la même tâche.
- Aucun changement aux tables existantes `conversations`, `profiles`, archives ou consentements.

### Phase B — travailleurs et quotas
- Ajouter une Edge Function de traitement avec limites de concurrence, délais et backoff.
- Ne pas appeler un modèle payant automatiquement.
- Utiliser uniquement les clés déjà configurées côté serveur ; aucune clé dans le navigateur.
- Prévoir un mode dégradé si Supabase ou un fournisseur est indisponible.

### Phase C — intégration Kofi
- Faire soumettre le texte via la file d'attente et afficher l'état en direct.
- Conserver le rendu pédagogique et LaTeX actuels.
- Préserver les circuits de vision/photo/PDF et les limites de 6 photos.
- Reprendre les tâches en attente après reconnexion ou rechargement de la page.

### Phase D — tests de charge et preuve
- 100, 500, 1 000, 2 000 et 3 000 soumissions simultanées : d'abord avec un faux fournisseur contrôlé, puis avec des essais limités sur les fournisseurs réels.
- Vérifier zéro perte, zéro doublon, isolation entre comptes et reprise après arrêt simulé d'un worker.
- Mesurer taux de succès, temps médian/P95, profondeur de file, 429, erreurs et coût facturé (doit rester 0).
- Tester sur Preview uniquement. Pas de fusion, déploiement Production, changement de forfait ni activation de fournisseur payant sans autorisation explicite.

## Conditions d'acceptation

La mention « prêt pour 3 000 soumissions simultanées » ne sera autorisée qu'après un test reproductible avec 3 000 comptes/tâches simulés, preuve que les 100 tâches sont conservées et que chacune aboutit à une réponse ou à un état d'échec récupérable. Ce test ne prouvera pas que les fournisseurs gratuits peuvent fournir 100 générations simultanées en temps réel ; cette limite doit rester visible dans la communication produit.

## Références officielles consultées

- Supabase Free pricing et quotas : https://supabase.com/pricing
- Supabase Edge Functions limits : https://supabase.com/docs/guides/functions/limits
- Render Free services limitations : https://render.com/docs/free
- Vercel Hobby plan and limits : https://vercel.com/docs/plans/hobby


## Registre initial des fournisseurs IA

- **Gemini API** : niveau gratuit disponible, mais quotas par projet/modèle et variables. Lire les limites exactes dans AI Studio pour les clés AFROACADEMY ; ne pas extrapoler une capacité garantie. Les conditions de traitement des données du niveau gratuit doivent être examinées avant usage de données d'élèves.
- **Groq** : plan gratuit avec limites par organisation et modèle. Lire les limites exactes dans la console du compte ; ne pas prendre un tableau tiers comme engagement de capacité.
- **Cloudflare Workers AI** : allocations gratuites limitées ; certains modèles peuvent être exclus du gratuit ou refuser les requêtes faute de capacité. Ne pas le traiter comme illimité.
- **OpenRouter modèles gratuits** : limites et disponibilité variables selon le fournisseur amont ; ne pas en faire le cœur d'une offre à haut volume.
- **Cerebras et autres crédits d'essai** : un crédit temporaire n'est pas une offre gratuite permanente.
- **Autres agrégateurs** : ne retenir que ceux qui apportent un quota propre, des fournisseurs différents ou une capacité contractuelle. Un simple proxy n'ajoute pas de puissance IA.

Sources consultées le 18 novembre 2026 :
- Gemini API : https://ai.google.dev/gemini-api/docs/pricing
- Cloudflare Workers AI : https://developers.cloudflare.com/changelog/product/workers-ai/
- Inventaire secondaire des offres gratuites (à confirmer sur les consoles officielles) : https://freeintelligence.ai/free-tiers/

Pour les demandes de mathématiques, le worker exécute aussi un contrôle déterministe ciblé des égalités numériques et de quelques erreurs symboliques connues. Si un problème est détecté, il demande une correction à un moteur, puis ne sauvegarde la réponse que si le second contrôle passe. Ce contrôle est un filet de sécurité ciblé, pas un logiciel de calcul formel général.\n\n## Limite de capacité et décision

Avec 0 USD/mois, la cible réaliste est **d'accepter et conserver jusqu'à 3 000 soumissions en rafale**, si les limites d'hébergement et de stockage le permettent, puis de les traiter progressivement. Il est impossible de garantir 3 000 corrections IA lourdes instantanées avec les quotas gratuits actuels. Pour garantir un débit de réponses, il faudra des quotas contractuels/payants ou des ressources de calcul dédiées. L'interface doit afficher l'attente et ne jamais faire croire qu'un exercice a été perdu.
