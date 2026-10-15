terraform {
  required_version = ">= 1.5.0, < 2.0.0"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "= 9.8.0"
    }
  }
}

# Resource Manager uses the owner's approved resource principal, not a copied PAT.
provider "oci" {
  region = var.home_region
  auth   = "ResourcePrincipal"
}

variable "home_region" {
  type = string
}
variable "selected_region" {
  type = string
}
variable "compartment_id" {
  type = string
}
variable "availability_domain" {
  type = string
}
variable "image_id" {
  type = string
}
variable "image_verified_always_free" {
  type = bool
  validation {
    condition     = var.image_verified_always_free
    error_message = "Verify an ARM Ubuntu image marked Always Free eligible in the owner console."
  }
}
variable "quoted_monthly_usd" {
  description = "Actual console-verified gross monthly total. Zero only; trial credits are not a free quote."
  type        = number
  validation {
    condition     = var.quoted_monthly_usd == 0
    error_message = "This module allows only a verified USD 0 recurring quote."
  }
}
variable "free_capacity" {
  description = "Remaining account-wide allowances after existing resources, independently verified before apply."
  type = object({
    ocpu_hours   = number
    ram_gb_hours = number
    boot_disk_gb = number
  })
  validation {
    condition     = var.free_capacity.ocpu_hours >= 1488 && var.free_capacity.ram_gb_hours >= 8928 && var.free_capacity.boot_disk_gb >= 50
    error_message = "One 2 OCPU/12 GiB host for 744 hours and its 50 GiB boot disk must fit the remaining free allowance."
  }
}
variable "ssh_public_key" {
  type = string
  validation {
    # The fixed SSH wire header encodes algorithm length/name and exactly 32 key bytes.
    condition     = can(regex("^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-P][A-Za-z0-9+/]{42}( .*)?$", var.ssh_public_key))
    error_message = "A dedicated deployment public key is required; never provide the private key."
  }
}
variable "ssh_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.ssh_cidrs) > 0 && alltrue([for cidr in var.ssh_cidrs : can(cidrhost(cidr, 0)) && can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+/32$", cidr))])
    error_message = "SSH ingress requires exact operator IPv4 /32 addresses."
  }
}

resource "oci_core_vcn" "acceptance" {
  compartment_id = var.compartment_id
  cidr_block     = "10.77.0.0/16"
  display_name   = "omnidesk-free-acceptance"
  dns_label      = "omnidesk"
}
resource "oci_core_internet_gateway" "acceptance" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.acceptance.id
  enabled        = true
  display_name   = "omnidesk-free-acceptance"
}
resource "oci_core_route_table" "acceptance" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.acceptance.id
  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.acceptance.id
  }
}
resource "oci_core_security_list" "acceptance" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.acceptance.id
  dynamic "ingress_security_rules" {
    for_each = var.ssh_cidrs
    content {
      protocol    = "6"
      source      = ingress_security_rules.value
      source_type = "CIDR_BLOCK"
      description = "Operator SSH only"
      stateless   = false
      tcp_options {
        min = 22
        max = 22
      }
    }
  }
  dynamic "ingress_security_rules" {
    for_each = [80, 443]
    content {
      protocol    = "6"
      source      = "0.0.0.0/0"
      source_type = "CIDR_BLOCK"
      description = "Public web only"
      stateless   = false
      tcp_options {
        min = ingress_security_rules.value
        max = ingress_security_rules.value
      }
    }
  }
  ingress_security_rules {
    protocol    = "1"
    source      = "0.0.0.0/0"
    source_type = "CIDR_BLOCK"
    description = "IPv4 path MTU discovery"
    stateless   = false
    icmp_options {
      type = 3
      code = 4
    }
  }
  dynamic "egress_security_rules" {
    for_each = [53, 80, 443]
    content {
      protocol         = "6"
      destination      = "0.0.0.0/0"
      destination_type = "CIDR_BLOCK"
      description      = "DNS and OS HTTPS access"
      stateless        = false
      tcp_options {
        min = egress_security_rules.value
        max = egress_security_rules.value
      }
    }
  }
  egress_security_rules {
    protocol         = "17"
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    description      = "DNS only"
    stateless        = false
    udp_options {
      min = 53
      max = 53
    }
  }
}
resource "oci_core_subnet" "acceptance" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.acceptance.id
  cidr_block                 = "10.77.1.0/24"
  route_table_id             = oci_core_route_table.acceptance.id
  security_list_ids          = [oci_core_security_list.acceptance.id]
  prohibit_public_ip_on_vnic = false
  dns_label                  = "acceptance"
}
resource "oci_core_instance" "acceptance" {
  compartment_id      = var.compartment_id
  availability_domain = var.availability_domain
  display_name        = "omnidesk-free-acceptance"
  shape               = "VM.Standard.A1.Flex"
  shape_config {
    ocpus         = 2
    memory_in_gbs = 12
  }
  create_vnic_details {
    subnet_id        = oci_core_subnet.acceptance.id
    assign_public_ip = true
  }
  source_details {
    source_type             = "image"
    source_id               = var.image_id
    boot_volume_size_in_gbs = 50
    boot_volume_vpus_per_gb = 10
  }
  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data           = filebase64("${path.module}/cloud-init.yaml")
  }
  preserve_boot_volume = true
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = var.home_region == var.selected_region
      error_message = "Free resources must be created in the account home region."
    }
  }
}
output "acceptance_ipv4" {
  value = oci_core_instance.acceptance.public_ip
}
