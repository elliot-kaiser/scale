-- Scale Hub — Supabase access for the Pi (anon key)
--
-- Your tables can already exist. This script fixes the usual "403 Forbidden"
-- problem: RLS enabled without policies, and/or anon missing table grants.
--
-- In Supabase: SQL Editor → New query → paste → Run.

-- 1) Grants (PostgREST needs these even with RLS policies)
grant usage on schema public to anon, authenticated;

grant select, insert, update, delete on table public.ingredients to anon, authenticated;
grant select, insert, update, delete on table public.daily_logs to anon, authenticated;
grant select, insert, update, delete on table public.weight_logs to anon, authenticated;
grant select, insert, update, delete on table public.recipes to anon, authenticated;
grant select, insert, update, delete on table public.recipe_items to anon, authenticated;
grant select, insert, update, delete on table public.user_targets to anon, authenticated;
grant select, insert, update, delete on table public.guided_sessions to anon, authenticated;

grant usage, select on all sequences in schema public to anon, authenticated;

-- 2) RLS + open policies for single-home / LAN scale (anon key on the Pi)
alter table public.ingredients enable row level security;
alter table public.daily_logs enable row level security;
alter table public.weight_logs enable row level security;
alter table public.recipes enable row level security;
alter table public.recipe_items enable row level security;
alter table public.user_targets enable row level security;
alter table public.guided_sessions enable row level security;

drop policy if exists "anon_ingredients" on public.ingredients;
create policy "anon_ingredients" on public.ingredients
  for all to anon, authenticated using (true) with check (true);

drop policy if exists "anon_daily_logs" on public.daily_logs;
create policy "anon_daily_logs" on public.daily_logs
  for all to anon, authenticated using (true) with check (true);

drop policy if exists "anon_weight_logs" on public.weight_logs;
create policy "anon_weight_logs" on public.weight_logs
  for all to anon, authenticated using (true) with check (true);

drop policy if exists "anon_recipes" on public.recipes;
create policy "anon_recipes" on public.recipes
  for all to anon, authenticated using (true) with check (true);

drop policy if exists "anon_recipe_items" on public.recipe_items;
create policy "anon_recipe_items" on public.recipe_items
  for all to anon, authenticated using (true) with check (true);

drop policy if exists "anon_user_targets" on public.user_targets;
create policy "anon_user_targets" on public.user_targets
  for all to anon, authenticated using (true) with check (true);

drop policy if exists "anon_guided_sessions" on public.guided_sessions;
create policy "anon_guided_sessions" on public.guided_sessions
  for all to anon, authenticated using (true) with check (true);
