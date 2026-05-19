terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    bucket = "tfstate-rag-portfolio-086769"
    key    = "terraform.tfstate"
    region = "ap-northeast-1"
  }
}

provider "aws" {
  region = "ap-northeast-1"
}

module "vpc" {
  source       = "./modules/vpc"
  project_name = "rag-portfolio"
}

module "s3" {
  source       = "./modules/s3"
  project_name = "rag-portfolio"
  account_id   = "086769945521"
}