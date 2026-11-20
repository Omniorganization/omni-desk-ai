terraform {
  required_version = "= 1.16.4"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "= 1.69.0"
    }
  }
}

# Credentials come exclusively from HCLOUD_TOKEN, never from tfvars or user_data.
provider "hcloud" {}

variable "name" {
  type    = string
  default = "omnidesk-acceptance"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,40}$", var.name))
    error_message = "Use a short lowercase resource name."
  }
}

variable "location" {
  type    = string
  default = "nbg1"
  validation {
    condition     = contains(["nbg1", "hel1"], var.location)
    error_message = "Only the quoted European CX23 locations are allowed; no paid fallback."
  }
}

variable "ssh_public_key" {
  type = string
  validation {
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/]+={0,3}( .*)?$", var.ssh_public_key))
    error_message = "Provide a dedicated deployment public key, never its private key."
  }
}

variable "ssh_cidrs" {
  type = list(string)
  validation {
    condition = length(var.ssh_cidrs) > 0 && alltrue([
      for cidr in var.ssh_cidrs : can(cidrhost(cidr, 0)) && can(regex("(/32|/128)$", cidr))
    ])
    error_message = "SSH requires explicit client IPv4 /32 or IPv6 /128 addresses."
  }
}

variable "server_type" {
  description = "cx23 (2 vCPU/4 GB, fits USD 10) or cpx22 (2 vCPU/4 GB AMD, about EUR 20/month with backups) when cx23 is out of stock and the owner authorized the higher budget."
  type        = string
  default     = "cx23"
  validation {
    condition     = contains(["cx23", "cpx22"], var.server_type)
    error_message = "Only the quoted cx23 or, with a raised authorization, cpx22 are allowed."
  }
}

variable "authorized_monthly_budget_usd" {
  description = "The owner's written monthly authorization for this host. Raise it only with a new owner approval."
  type        = number
  default     = 10
  validation {
    condition     = var.authorized_monthly_budget_usd > 0 && var.authorized_monthly_budget_usd <= 30
    error_message = "The authorization must be positive and at most USD 30 for this single host."
  }
}

variable "quoted_total_usd" {
  description = "Console-verified MONTHLY GROSS total: server, IPv4, backups, tax and currency conversion. This is a quote gate, not a billing cap."
  type        = number
  validation {
    condition     = var.quoted_total_usd > 0 && var.quoted_total_usd <= var.authorized_monthly_budget_usd
    error_message = "The complete monthly quote must be positive and within the owner's authorized monthly budget."
  }
}

variable "ntp_servers" {
  description = "NTP servers (UDP 123). Signed runner requests, device signatures, approval expiry and TLS need a synchronised clock. Defaults are ntp1-3.hetzner.de."
  type        = list(string)
  default     = ["213.239.239.164/32", "213.239.239.165/32", "213.239.239.166/32"]
  validation {
    condition     = length(var.ntp_servers) > 0 && alltrue([for cidr in var.ntp_servers : can(cidrhost(cidr, 0)) && can(regex("(/32|/128)$", cidr))])
    error_message = "List NTP servers as exact /32 or /128 addresses."
  }
}

variable "enable_wireguard" {
  description = "Open UDP 51820 for the CI deploy tunnel. WireGuard answers nothing without a valid peer key, so SSH stays limited to ssh_cidrs."
  type        = bool
  default     = false
}

variable "outbound_cidrs" {
  description = "Owner-approved destination networks for DNS, OS updates and HTTPS; unrestricted default routes are forbidden."
  type        = list(string)
  validation {
    condition     = length(var.outbound_cidrs) > 0 && alltrue([for cidr in var.outbound_cidrs : can(cidrhost(cidr, 0)) && !can(regex("/0+$", cidr))])
    error_message = "Supply approved outbound destination CIDRs; empty lists and IPv4/IPv6 default routes are forbidden."
  }
}

resource "hcloud_ssh_key" "deployer" {
  name       = "${var.name}-deployer"
  public_key = var.ssh_public_key
}

resource "hcloud_firewall" "acceptance" {
  name = "${var.name}-ingress"
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = var.ssh_cidrs
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  dynamic "rule" {
    for_each = ["53", "80", "443"]
    content {
      direction       = "out"
      protocol        = "tcp"
      port            = rule.value
      destination_ips = var.outbound_cidrs
    }
  }
  rule {
    direction       = "out"
    protocol        = "udp"
    port            = "53"
    destination_ips = var.outbound_cidrs
  }
  rule {
    direction       = "out"
    protocol        = "udp"
    port            = "123"
    destination_ips = var.ntp_servers
  }
  dynamic "rule" {
    for_each = var.enable_wireguard ? ["51820"] : []
    content {
      direction  = "in"
      protocol   = "udp"
      port       = rule.value
      source_ips = ["0.0.0.0/0", "::/0"]
    }
  }
}

resource "hcloud_server" "acceptance" {
  name               = var.name
  server_type        = var.server_type
  image              = "ubuntu-24.04"
  location           = var.location
  ssh_keys           = [hcloud_ssh_key.deployer.id]
  firewall_ids       = [hcloud_firewall.acceptance.id]
  backups            = true
  delete_protection  = true
  rebuild_protection = true
  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }
  labels = {
    project = "omnidesk"
    purpose = "single-node-acceptance"
  }
  user_data = file("${path.module}/cloud-init.yaml")
  lifecycle {
    prevent_destroy = true
  }
}

output "acceptance_ipv4" {
  value = hcloud_server.acceptance.ipv4_address
}

output "scope" {
  value = "Persistent single host only. No application deployment, HA, production signature or GA evidence is implied."
}
