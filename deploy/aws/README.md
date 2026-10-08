# Amazon RDS certificate authorities

`rds-global-bundle.pem` is Amazon RDS's global CA bundle, so the image can reach a managed
PostgreSQL with `PGSSLMODE=verify-full` (`PGSSLROOTCERT=/etc/ssl/certs/rds-global-bundle.pem`).

| | |
|---|---|
| Source | `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem` (public; AWS documentation, "Using SSL/TLS to encrypt a connection to a DB instance") |
| Retrieved | 2026-10-08 |
| SHA-256 | `fe45bbebf92ad3e27a583bbb2ddd1553c521ed4d49af5514dc0a40372ea5395c` |
| Size | 169,984 bytes; 111 root certificates (RSA2048 G1 roots valid to 2061; RSA4096 and ECC384 G1 to 2121; one set per region) |

Certificates only: no private key. Replace it by fetching the same URL, checking the
certificates (`openssl x509 -noout -subject -enddate`), and updating the SHA-256 here and in
`scripts/image_smoke.sh`; that is an image change, to be re-qualified like any other.
