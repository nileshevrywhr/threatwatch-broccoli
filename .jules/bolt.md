## 2024-05-24 - Supabase/PostgREST Resource Embedding
**Learning:** Supabase (via PostgREST) supports "Resource Embedding" to fetch related data in a single request, similar to a JOIN. This avoids N+1 query problems. The syntax is `select("*, related_table(column)")`.

**Action:** When fetching data that relies on foreign key relationships, always check if resource embedding can be used instead of separate queries.
