-- 1) Signal outcomes today: how many placed / rejected / dropped / cancels processed
select coalesce(state, 'entry') as kind, outcome, count(*) as n
from news_reactor_signals
where (received_at at time zone 'America/New_York')::date = (now() at time zone 'America/New_York')::date
group by 1, 2
order by 1, 2;

-- 2) Every signal with its result and (for entries) the order(s) it produced
select (s.received_at at time zone 'America/New_York')::time(0) as received_et,
       s.source_event_id, coalesce(s.state, 'entry') as kind, s.symbol, s.direction,
       s.outcome, s.reason,
       o.entry_order_id, o.strategy, o.expiration, o.attempt, o.status as order_status, o.status_reason
from news_reactor_signals s
left join torque_orders o on o.source_event_id = s.source_event_id
order by s.received_at desc, o.attempt;

-- 3) Order status breakdown
select status, count(*) as n
from torque_orders
group by status
order by n desc;

-- 4) Orders still live at the broker (not yet closed out)
select entry_order_id, source_event_id, symbol, strategy, expiration, attempt, status,
       (placed_at at time zone 'America/New_York')::time(0) as placed_et
from torque_orders
where status in ('pending', 'open', 'partially_filled', 'submitted')
order by placed_at desc;

-- 5) Rejections and drops, with the reason (signal level)
select (received_at at time zone 'America/New_York')::time(0) as received_et,
       source_event_id, coalesce(state, 'entry') as kind, symbol, direction, outcome, reason
from news_reactor_signals
where outcome in ('rejected', 'dropped')
order by received_at desc;

-- 6) Cancels: what each cancel signal targeted and what happened to those orders
select (c.received_at at time zone 'America/New_York')::time(0) as cancel_et,
       c.source_event_id as cancel_id, c.cancels_event_id, c.outcome, c.reason,
       o.entry_order_id, o.status as order_status, o.status_reason
from news_reactor_signals c
left join torque_orders o on o.source_event_id = c.cancels_event_id
where c.state = 'cancel'
order by c.received_at desc;

-- 7) Signals that retried on a later expiry, and how the retry ended
select source_event_id, entry_order_id, expiration, attempt, status, status_reason
from torque_orders
where source_event_id in (select source_event_id from torque_orders where attempt > 1)
order by source_event_id, attempt;

-- 8) Full raw payload for one signal (replace the id)
select source_event_id, payload, outcome, reason
from news_reactor_signals
where source_event_id = 'nr-9500000023-51fd9211';