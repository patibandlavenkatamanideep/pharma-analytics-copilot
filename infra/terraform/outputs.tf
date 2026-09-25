output "public_ip" {
  description = "Elastic IP. Stable across instance restarts, which matters because the hostname is derived from it."
  value       = aws_eip.app.public_ip
}

output "public_host" {
  description = <<-EOT
    The sslip.io hostname Caddy requests a certificate for. sslip.io resolves
    a dashed IP to that IP, so a real Let's Encrypt certificate can be issued
    without owning a domain.
  EOT
  value       = "${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io"
}

output "url" {
  description = "Where the application will be reachable once the first boot finishes."
  value       = "https://${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io"
}

output "next_steps" {
  description = "What still has to be run on the host after apply."
  value       = <<-EOT
    The instance clones the repository and installs Docker on first boot, but
    it does NOT load data or issue logins -- both need decisions.

      ssh -i ~/.ssh/${var.key_name}.pem ec2-user@${aws_eip.app.public_ip}
      cd pharma-analytics-copilot
      export PAC_PUBLIC_HOST=${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io
      docker compose -f compose.yaml -f compose.prod.yaml up -d --build
      docker compose exec app python3 scripts/bootstrap_db.py
      docker compose exec app python3 scripts/load_data.py --mode full
      docker compose exec app python3 scripts/provision_logins.py --demo
      ./infra/smoke.sh https://${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io
  EOT
}
