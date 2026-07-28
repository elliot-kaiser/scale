-- Scale Hub schema (keep this — do NOT replace with the older UUID version)
-- If tables already exist from running this script, you are done.
-- Only re-run the DROP + CREATE block if you intentionally want a wipe.

-- Optional: enable RLS policies so the anon key can read/write from the Pi app.
-- Run this AFTER your tables exist.

alter table public.ingredients enable row level security;
alter table public.daily_logs enable row level security;
alter table public.weight_logs enable row level security;
alter table public.recipes enable row level security;
alter table public.recipe_items enable row level security;
alter table public.user_targets enable row level security;
alter table public.guided_sessions enable row level security;

drop policy if exists "anon_ingredients" on public.ingredients;
create policy "anon_ingredients" on public.ingredients
  for all using (true) with check (true);

drop policy if exists "anon_daily_logs" on public.daily_logs;
create policy "anon_daily_logs" on public.daily_logs
  for all using (true) with check (true);

drop policy if exists "anon_weight_logs" on public.weight_logs;
create policy "anon_weight_logs" on public.weight_logs
  for all using (true) with check (true);

drop policy if exists "anon_recipes" on public.recipes;
create policy "anon_recipes" on public.recipes
  for all using (true) with check (true);

drop policy if exists "anon_recipe_items" on public.recipe_items;
create policy "anon_recipe_items" on public.recipe_items
  for all using (true) with check (true);

drop policy if exists "anon_user_targets" on public.user_targets;
create policy "anon_user_targets" on public.user_targets
  for all using (true) with check (true);

drop policy if exists "anon_guided_sessions" on public.guided_sessions;
create policy "anon_guided_sessions" on public.guided_sessions
  for all using (true) with check (true);
