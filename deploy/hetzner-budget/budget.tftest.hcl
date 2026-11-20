mock_provider "hcloud" {}

variables {
  ssh_public_key   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7txPvTBVoEV4RJwdMHngFx2JJsXeUgDoqhcaWMjGFN test-only"
  ssh_cidrs        = ["192.0.2.17/32"]
  outbound_cidrs   = ["198.51.100.0/24", "2001:db8:1::/64"]
  quoted_total_usd = 9.50
}

run "restricted_persistent_single_host" {
  command = plan
  assert {
    condition     = hcloud_server.acceptance.server_type == "cx23" && hcloud_server.acceptance.backups && hcloud_server.acceptance.delete_protection && hcloud_server.acceptance.rebuild_protection
    error_message = "The budget host must retain backups and deletion protection without a paid type substitution."
  }
  assert {
    condition     = length([for rule in hcloud_firewall.acceptance.rule : rule if rule.direction == "in"]) == 3 && alltrue([for rule in hcloud_firewall.acceptance.rule : rule.direction != "in" || contains(["22", "80", "443"], rule.port)])
    error_message = "Database, gateway, sandbox and runtime sockets must not be exposed by the cloud firewall."
  }
  assert {
    condition     = alltrue([for rule in hcloud_firewall.acceptance.rule : rule.port != "22" || rule.source_ips == toset(var.ssh_cidrs)])
    error_message = "SSH must remain restricted to approved client IPs."
  }
  assert {
    condition     = length([for rule in hcloud_firewall.acceptance.rule : rule if rule.direction == "out"]) == 5 && alltrue([for rule in hcloud_firewall.acceptance.rule : rule.direction != "out" || (rule.destination_ips == toset(var.outbound_cidrs) && (rule.protocol == "tcp" ? contains(["53", "80", "443"], rule.port) : rule.protocol == "udp" && rule.port == "53")) || (rule.protocol == "udp" && rule.port == "123" && rule.destination_ips == toset(var.ntp_servers))])
    error_message = "Outbound access must be limited to approved destinations (TCP DNS/HTTP/HTTPS, UDP DNS) plus UDP NTP to the listed time servers."
  }
  assert {
    condition     = length([for rule in hcloud_firewall.acceptance.rule : rule if rule.direction == "in" && rule.protocol == "udp"]) == 0
    error_message = "Without enable_wireguard no UDP port may be open inbound."
  }
  assert {
    condition     = hcloud_server.acceptance.server_type == "cx23"
    error_message = "The default host stays the quoted cx23."
  }
}

run "wireguard_opens_only_udp_51820" {
  command = plan
  variables {
    enable_wireguard = true
  }
  assert {
    condition     = length([for rule in hcloud_firewall.acceptance.rule : rule if rule.direction == "in"]) == 4 && length([for rule in hcloud_firewall.acceptance.rule : rule if rule.direction == "in" && rule.protocol == "udp" && rule.port == "51820"]) == 1
    error_message = "enable_wireguard must add exactly one inbound rule: UDP 51820."
  }
  assert {
    condition     = alltrue([for rule in hcloud_firewall.acceptance.rule : rule.port != "22" || rule.source_ips == toset(var.ssh_cidrs)])
    error_message = "WireGuard must not widen SSH."
  }
}

run "fallback_type_needs_raised_authorization" {
  command = plan
  variables {
    server_type                   = "cpx22"
    authorized_monthly_budget_usd = 25
    quoted_total_usd              = 24.10
  }
  assert {
    condition     = hcloud_server.acceptance.server_type == "cpx22"
    error_message = "cpx22 is allowed when the owner raised the authorization."
  }
}

run "reject_quote_above_authorization" {
  command = plan
  variables {
    server_type      = "cpx22"
    quoted_total_usd = 24.10
  }
  expect_failures = [var.quoted_total_usd]
}

run "reject_unquoted_server_type" {
  command = plan
  variables {
    server_type = "ccx13"
  }
  expect_failures = [var.server_type]
}

run "reject_ntp_ranges" {
  command = plan
  variables {
    ntp_servers = ["213.239.239.0/24"]
  }
  expect_failures = [var.ntp_servers]
}

run "reject_unknown_outbound_destinations" {
  command = plan
  variables {
    outbound_cidrs = []
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_unrestricted_ipv4_outbound" {
  command = plan
  variables {
    outbound_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_unrestricted_ipv6_outbound" {
  command = plan
  variables {
    outbound_cidrs = ["::/0"]
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_unrestricted_ipv4_outbound_zero_padded_prefix" {
  command = plan
  variables {
    outbound_cidrs = ["0.0.0.0/00"]
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_unrestricted_ipv6_outbound_zero_padded_prefix" {
  command = plan
  variables {
    outbound_cidrs = ["::/000"]
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_over_budget" {
  command = plan
  variables {
    quoted_total_usd = 10.01
  }
  expect_failures = [var.quoted_total_usd]
}

run "reject_unknown_quote" {
  command = plan
  variables {
    quoted_total_usd = 0
  }
  expect_failures = [var.quoted_total_usd]
}

run "reject_public_ssh" {
  command = plan
  variables {
    ssh_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.ssh_cidrs]
}

run "reject_unquoted_region" {
  command = plan
  variables {
    location = "sin"
  }
  expect_failures = [var.location]
}
