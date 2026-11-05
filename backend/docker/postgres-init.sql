-- Disposable database for the backend test suite. The test runner drops and
-- recreates tables here on every test; it must never point at POSTGRES_DB.
CREATE DATABASE voice_test;
