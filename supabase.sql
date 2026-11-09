-- Run this in Supabase: Dashboard > SQL Editor > New query > paste > Run.
-- Safe to re-run (e.g. after updating the app): it never deletes anyone's alerts.

create extension if not exists pgcrypto;

-- ---------- validation helpers ----------
create or replace function public.valid_urls(arr text[]) returns boolean
language sql immutable as $$
  select coalesce(bool_and(u ~* '^https?://[^\s/]+[^\s]*$' and length(u) <= 500), true)
  from unnest(arr) u
$$;

create or replace function public.valid_words(arr text[]) returns boolean
language sql immutable as $$
  select coalesce(bool_and(length(trim(w)) between 1 and 60), true)
  from unnest(arr) w
$$;

create or replace function public.valid_ids(arr text[]) returns boolean
language sql immutable as $$
  select coalesce(bool_and(i ~ '^[a-z0-9_-]{1,40}$'), true)
  from unnest(arr) i
$$;

-- ---------- alert settings: one row per signed-in user ----------
create table if not exists public.alerts (
  user_id    uuid primary key default auth.uid() references auth.users on delete cascade,
  email      text not null default (auth.jwt() ->> 'email'),
  created_at timestamptz not null default now()
);

alter table public.alerts
  add column if not exists sites          text[] not null default '{}',  -- the person's own extra sites/feeds
  add column if not exists sources        text[] not null default '{}',  -- ids of ready-made sources (docs/presets.json)
  add column if not exists triggers       text[] not null default '{}',  -- ids of sales triggers (docs/presets.json)
  add column if not exists keywords       text[] not null default '{}',
  add column if not exists exclude        text[] not null default '{}',
  add column if not exists frequency      text   not null default 'instant',
  add column if not exists paused         boolean not null default false,
  add column if not exists token          uuid   not null default gen_random_uuid(),
  add column if not exists last_digest_at timestamptz;

alter table public.alerts
  alter column sites set default '{}',
  alter column keywords set default '{}';

-- (re)create the checks so re-running always applies the latest rules
alter table public.alerts
  drop constraint if exists alerts_sites_check,
  drop constraint if exists alerts_keywords_check,
  drop constraint if exists alerts_exclude_check,
  drop constraint if exists alerts_lists_check,
  drop constraint if exists alerts_frequency_check,
  drop constraint if exists alerts_something_to_watch,
  drop constraint if exists alerts_something_to_match;

alter table public.alerts
  add constraint alerts_lists_check check (
    cardinality(sites) <= 10 and public.valid_urls(sites)
    and cardinality(sources) <= 60 and public.valid_ids(sources)
    and cardinality(triggers) <= 20 and public.valid_ids(triggers)
    and cardinality(keywords) <= 30 and public.valid_words(keywords)
    and cardinality(exclude) <= 30 and public.valid_words(exclude)),
  add constraint alerts_frequency_check check (frequency in ('instant', 'daily', 'weekly')),
  add constraint alerts_something_to_watch check (cardinality(sites) + cardinality(sources) > 0),
  add constraint alerts_something_to_match check (cardinality(keywords) + cardinality(triggers) > 0);

-- Signed-in users can only see and change their own row, and only these columns.
alter table public.alerts enable row level security;
revoke all on public.alerts from anon, authenticated;
grant select on public.alerts to authenticated;
grant insert (sites, sources, triggers, keywords, exclude, frequency, paused) on public.alerts to authenticated;
grant update (sites, sources, triggers, keywords, exclude, frequency, paused) on public.alerts to authenticated;
grant delete on public.alerts to authenticated;

drop policy if exists "own alerts" on public.alerts;
create policy "own alerts" on public.alerts
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- ---------- findings: every matching story, kept for 90 days ----------
create table if not exists public.findings (
  id         bigint generated always as identity primary key,
  user_id    uuid not null references public.alerts (user_id) on delete cascade,
  story_id   text not null,
  title      text not null,
  link       text not null,
  source     text not null,
  triggers   text[] not null default '{}',
  keyword    text,
  relevance  smallint not null default 0,   -- 3 very high, 2 high, 1 medium, 0 keyword only
  sent       boolean not null default false,
  found_at   timestamptz not null default now(),
  unique (user_id, story_id)
);
create index if not exists findings_unsent on public.findings (user_id) where not sent;

-- People can read and dismiss their own findings; only the checker adds them.
alter table public.findings enable row level security;
revoke all on public.findings from anon, authenticated;
grant select, delete on public.findings to authenticated;

drop policy if exists "own findings" on public.findings;
create policy "own findings" on public.findings
  for all to authenticated
  using (user_id = auth.uid());

-- ---------- one-click unsubscribe from alert emails (no login needed) ----------
create or replace function public.unsubscribe(t uuid) returns boolean
language sql security definer set search_path = public as $$
  with d as (delete from alerts where token = t returning 1)
  select exists (select 1 from d)
$$;
revoke execute on function public.unsubscribe(uuid) from public;
grant execute on function public.unsubscribe(uuid) to anon, authenticated;

-- If you ran the very first version of this file, tidy it up (uncomment):
-- drop table if exists public.subscriptions;
-- drop function if exists public.confirm_subscription(uuid);
