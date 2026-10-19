-- 013 去掉 postgis 镜像在 template1 里自动创建的多余扩展（§4 明确 postgis_tiger_geocoder 不装：
--     它会导入大量美国地址数据，对东南亚/非洲无用且占空间；topology 与 fuzzystrmatch 亦不在 v1 清单）
DROP EXTENSION IF EXISTS postgis_tiger_geocoder CASCADE;
DROP EXTENSION IF EXISTS postgis_topology;
DROP EXTENSION IF EXISTS fuzzystrmatch;
DROP SCHEMA IF EXISTS tiger CASCADE;
DROP SCHEMA IF EXISTS tiger_data CASCADE;
SELECT extname, extversion FROM pg_extension ORDER BY extname;
