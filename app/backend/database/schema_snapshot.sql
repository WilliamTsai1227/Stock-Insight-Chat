-- 把 public schema 的結構輸出成「排序過、與建立順序無關」的一行一筆，用來比對正式 RDS／本機／規格。
-- 用法見 specifications/sql_dev_handbook.md §5。
SELECT line FROM (
  SELECT 'TABLE    ' || c.relname AS line
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
  UNION ALL
  SELECT 'COLUMN   ' || c.table_name || '.' || c.column_name || '  ' ||
         CASE WHEN c.data_type = 'USER-DEFINED' THEN c.udt_name
              WHEN c.character_maximum_length IS NOT NULL THEN c.data_type || '(' || c.character_maximum_length || ')'
              WHEN c.data_type = 'numeric' AND c.numeric_precision IS NOT NULL THEN 'numeric(' || c.numeric_precision || ',' || c.numeric_scale || ')'
              ELSE c.data_type END ||
         CASE WHEN c.is_nullable = 'NO' THEN ' NOT NULL' ELSE '' END ||
         COALESCE(' DEFAULT ' || c.column_default, '')
    FROM information_schema.columns c
   WHERE c.table_schema = 'public'
  UNION ALL
  SELECT 'CONSTR   ' || rel.relname || '.' || con.conname || '  ' || pg_get_constraintdef(con.oid, true)
    FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = rel.relnamespace
   WHERE n.nspname = 'public'
     AND con.contype <> 'n'                          -- PG18 會把 NOT NULL 列成約束；欄位那邊已經比過
  UNION ALL
  SELECT 'INDEX    ' || i.tablename || '.' || i.indexname || '  ' ||
         regexp_replace(i.indexdef, '^CREATE (UNIQUE )?INDEX \S+ ON (ONLY )?', '\1')
    FROM pg_indexes i WHERE i.schemaname = 'public'
  UNION ALL
  SELECT 'FUNC     ' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')  ' ||
         md5(regexp_replace(pg_get_functiondef(p.oid), '\s+', ' ', 'g'))
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = 'public' AND p.prokind = 'f'
  UNION ALL
  SELECT 'TRIGGER  ' || c.relname || '.' || t.tgname || '  ' ||
         regexp_replace(pg_get_triggerdef(t.oid, true), '^CREATE (OR REPLACE )?TRIGGER \S+ ', '')
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND NOT t.tgisinternal
  UNION ALL
  SELECT 'EXT      ' || extname FROM pg_extension WHERE extname <> 'plpgsql'
  UNION ALL
  SELECT 'SEQ      ' || sequencename FROM pg_sequences WHERE schemaname = 'public'
) x
ORDER BY line;
