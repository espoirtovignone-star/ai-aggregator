# AFROACADEMY — plan de capacité gratuite pour 100 élèves

## Décision de conception

Objectif : accepter les exercices de 100 élèves connectés, conserver chaque demande, la traiter avec une capacité contrôlée et restituer la réponse au bon élève. Le système ne doit jamais annoncer une réponse terminée avant qu'elle soit enregistrée.

**Contrainte non négociable : budget d'infrastructure = 0 USD/mois.** Cette contrainte permet de préparer une file d'attente durable et une meilleure gestion de concurrence, mais ne permet pas de garantir 100 générations IA simultanées : les quotas gratuits des fournisseurs restent externes, variables et partagés. En cas de saturation, les demandes doivent rester en attente et être reprises, pas être perdues.

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

## Politique de service pour 100 élèves

- Accepter jusqu'à 100 demandes simultanées si les limites de requêtes et de stockage le permettent.
- Traiter en parallèle uniquement jusqu'au budget de concurrence et de débit réellement autorisé par les fournisseurs.
- Les autres demandes restent dans une file visible et durable, avec leur position approximative et un état de progression.
- Ne jamais promettre 100 réponses instantanées ou un délai fixe avec des API gratuites.
- Si tous les fournisseurs gratuits sont en panne ou à quota, garder les exercices en attente et afficher une explication honnête.
- Prévoir des limites anti-abus par compte pour empêcher un élève de monopoliser les quotas partagés.

## Phases d'implémentation et critères de validation

### Phase A — fondation persistante
- Ajouter migrations SQL non destructives pour `kofi_jobs`, `kofi_provider_windows` et les fonctions SQL de claim/complete/retry.
- Ajouter tests RLS : élève A ne peut ni lire ni modifier la tâche de B.
- Ajouter tests de concurrence : 100 claims simultanés ne donnent jamais deux travailleurs pour la même tâche.
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
- 1, 10, 50 et 100 soumissions simultanées.
- Vérifier zéro perte, zéro doublon, isolation entre comptes et reprise après arrêt simulé d'un worker.
- Mesurer taux de succès, temps médian/P95, profondeur de file, 429, erreurs et coût facturé (doit rester 0).
- Tester sur Preview uniquement. Pas de fusion, déploiement Production, changement de forfait ni activation de fournisseur payant sans autorisation explicite.

## Conditions d'acceptation

La mention « prêt pour 100 élèves » ne sera autorisée qu'après un test reproductible avec 100 comptes/tâches simulés, preuve que les 100 tâches sont conservées et que chacune aboutit à une réponse ou à un état d'échec récupérable. Ce test ne prouvera pas que les fournisseurs gratuits peuvent fournir 100 générations simultanées en temps réel ; cette limite doit rester visible dans la communication produit.

## Références officielles consultées

- Supabase Free pricing et quotas : https://supabase.com/pricing
- Supabase Edge Functions limits : https://supabase.com/docs/guides/functions/limits
- Render Free services limitations : https://render.com/docs/free
- Vercel Hobby plan and limits : https://vercel.com/docs/plans/hobby
