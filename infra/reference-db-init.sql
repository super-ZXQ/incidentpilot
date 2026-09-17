CREATE ROLE reference_readonly LOGIN PASSWORD 'reference_readonly';
GRANT CONNECT ON DATABASE orders TO reference_readonly;
GRANT USAGE ON SCHEMA public TO reference_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO reference_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE reference IN SCHEMA public
    GRANT SELECT ON TABLES TO reference_readonly;
ALTER ROLE reference_readonly SET default_transaction_read_only = on;
ALTER ROLE reference_readonly SET statement_timeout = '5s';
