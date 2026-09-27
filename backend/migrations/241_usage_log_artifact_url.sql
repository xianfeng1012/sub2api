-- 使用记录的【制品】列：异步媒体任务（如 Seedance 视频）完成后的回看地址。
-- 仅在状态轮询首次观察到 succeeded 时写入，其他请求为 NULL。
ALTER TABLE usage_logs
    ADD COLUMN IF NOT EXISTS artifact_url TEXT;

COMMENT ON COLUMN usage_logs.artifact_url IS '异步媒体任务完成后的制品直链（如 Seedance 视频）；未产出制品时为 NULL'
