-- 011 seed：首发三国 + 已定商业数字（Task-Plan.md §12.1 / §14.2-21 / §14.2-22）
-- 幂等：全部 ON CONFLICT DO NOTHING
BEGIN;

-- 1) 地区配置单一来源（禁止代码写死国家判断）
INSERT INTO core.region (country_code, currency, timezone, features, status) VALUES
 ('TH','THB','Asia/Bangkok',
  '{"ride_categories":["tricycle","economy","comfort","van"],"ride_enabled":true,
    "payment_channels":["qr_transfer","operator_static","promptpay"],"points_pack_purchase":false,
    "deposit_enabled":true,"used_car_listing":true,"kyc_level_for_points":2,
    "privacy_regime":"PDPA","data_localization_required":false}'::jsonb,'active'),
 ('ZA','ZAR','Africa/Johannesburg',
  '{"ride_categories":["economy","comfort","van"],"ride_enabled":true,
    "payment_channels":["qr_transfer","operator_static","card_stripe"],"points_pack_purchase":false,
    "deposit_enabled":true,"used_car_listing":true,"kyc_level_for_points":2,
    "privacy_regime":"POPIA","data_localization_required":false}'::jsonb,'active'),
 ('NG','NGN','Africa/Lagos',
  '{"ride_categories":["tricycle","economy","van"],"ride_enabled":true,
    "payment_channels":["qr_transfer","operator_static","mpesa"],"points_pack_purchase":false,
    "deposit_enabled":true,"used_car_listing":true,"kyc_level_for_points":2,
    "privacy_regime":"NDPR","data_localization_required":false}'::jsonb,'active'),
 ('CN','CNY','Asia/Shanghai',
  '{"ride_categories":[],"ride_enabled":false,"payment_channels":["qr_transfer"],
    "points_pack_purchase":false,"deposit_enabled":false,"used_car_listing":false,
    "kyc_level_for_points":2,"note":"仅演示/开发用，不承载真实运营"}'::jsonb,'active')
ON CONFLICT (country_code) DO NOTHING;

-- 2) 佣金与分账默认规则（版本化；改率不追溯历史单）
INSERT INTO fin.settlement_rule (rule_type, subject, region_id, type_code, ratio, version, effective_from, status) VALUES
 ('commission','vehicle',      NULL, NULL, '{"platform":0.02}'::jsonb,               1, DATE '2026-01-01','active'),
 ('commission','part',         NULL, NULL, '{"platform":0.02}'::jsonb,               1, DATE '2026-01-01','active'),
 ('commission','used_vehicle', NULL, NULL, '{"platform":0.01}'::jsonb,               1, DATE '2026-01-01','active'),
 ('fare_split','ride_franchise',NULL,NULL,'{"driver":0.8,"operator":0.1,"platform":0.1}'::jsonb, 1, DATE '2026-01-01','active'),
 ('fare_split','ride_asset',   NULL, NULL, '{"driver":0.6,"operator":0.3,"platform":0.1}'::jsonb, 1, DATE '2026-01-01','active')
ON CONFLICT DO NOTHING;

-- 3) v1 收款线（真实渠道 M13 后逐个接入，改行不改代码）
INSERT INTO fin.payment_channel (channel_code, provider_type, regions, currencies, collect_mode, account_entity, status) VALUES
 ('qr_transfer_th','qr_transfer','["TH"]','["THB"]','platform_online','platform','on'),
 ('qr_transfer_za','qr_transfer','["ZA"]','["ZAR"]','platform_online','platform','on'),
 ('qr_transfer_ng','qr_transfer','["NG"]','["NGN"]','platform_online','platform','on'),
 ('dealer_offline_voucher','offline_voucher','["TH","ZA","NG"]','["THB","ZAR","NGN"]','dealer_offline','dealer','on'),
 ('operator_static_qr','qr_static','["TH","ZA","NG"]','["THB","ZAR","NGN"]','operator_offline','operator','on')
ON CONFLICT (channel_code) DO NOTHING;

-- 4) 派单策略默认版本（硬过滤 + 权重，可按运营商/区域覆盖，可 A/B）
INSERT INTO mobility.dispatch_policy (region_id, version, hard_rules, weights, offer_timeout_s, max_retry, enabled, status)
SELECT r.country_code, 1,
 '{"require_license":true,"require_fleet_contract":true,"require_operation_permit":true,
   "inside_operating_fence":true,"max_eta_s":900,"detour_max_pct":30,"detour_max_min":8,
   "blacklist_check":true,"self_dealing_pair_check":true}'::jsonb,
 '{"eta":0.40,"safety_score":0.20,"service_score":0.15,"accept_rate":0.10,
   "cancel_rate":0.10,"wait_fairness":0.05}'::jsonb,
 15, 3, true, 'active'
FROM core.region r
WHERE r.country_code IN ('TH','ZA','NG')
ON CONFLICT (region_id, version) DO NOTHING;

COMMIT;
