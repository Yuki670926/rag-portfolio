terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    github = {
      source  = "integrations/github"
      version = ">= 5.0"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "rag-portfolio"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = "Yuki670926"
    }
  }
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "rag-portfolio"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = "Yuki670926"
    }
  }
}

provider "github" {
  owner = "Yuki670926"
}

locals {
  project_name = "rp-${var.environment}"
}

module "vpc" {
  source               = "github.com/Yuki670926/rag-portfolio-modules//vpc?ref=v1.9.8"
  project_name         = local.project_name
  enable_vpc_endpoints = var.enable_vpc_endpoints
}

module "s3" {
  source            = "github.com/Yuki670926/rag-portfolio-modules//s3?ref=v2.0.1"
  project_name      = local.project_name
  account_id        = var.account_id
  ingest_lambda_arn = module.lambda.ingest_lambda_arn
  cloudfront_domain = module.cloudfront.distribution_domain_name
  kms_key_arn       = module.kms.s3_kms_key_arn
}

module "cognito" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//cognito?ref=v1.1.2"
  project_name = local.project_name
  environment  = var.environment
  admin_email  = "test@example.com"
}

module "lambda" {
  source                   = "github.com/Yuki670926/rag-portfolio-modules//lambda?ref=v1.9.8"
  project_name             = local.project_name
  documents_bucket_arn     = module.s3.documents_bucket_arn
  aws_region               = var.aws_region
  cognito_user_pool_id     = module.cognito.user_pool_id
  cognito_client_id        = module.cognito.user_pool_client_id
  conversations_table_name = module.dynamodb.conversations_table_name
  sessions_table_name      = module.dynamodb.sessions_table_name
  vector_store_type        = var.vector_store_type
  environment              = var.environment
  ingest_dlq_arn           = module.dlq_ingest.dlq_arn
  subnet_ids               = module.vpc.private_subnet_ids       # 追加
  lambda_security_group_id = module.vpc.lambda_security_group_id # 追加
}

module "opensearch" {
  count                  = var.vector_store_type == "opensearch" ? 1 : 0
  source                 = "github.com/Yuki670926/rag-portfolio-modules//opensearch?ref=v1.9.3"
  project_name           = local.project_name
  ingest_lambda_role_arn = module.lambda.ingest_lambda_role_arn
  query_lambda_role_arn  = module.lambda.query_lambda_role_arn
}

module "api_gateway" {
  source                       = "github.com/Yuki670926/rag-portfolio-modules//api_gateway?ref=v2.0.2"
  project_name                 = local.project_name
  cognito_user_pool_arn        = module.cognito.user_pool_arn
  query_lambda_arn             = module.lambda.query_lambda_arn
  query_lambda_invoke_arn      = module.lambda.query_lambda_invoke_arn
  cloudfront_domain            = module.cloudfront.distribution_domain_name
  cognito_user_pool_id         = module.cognito.user_pool_id
  cognito_client_id            = module.cognito.user_pool_client_id
  authorizer_lambda_invoke_arn = module.lambda.authorizer_lambda_invoke_arn
  authorizer_lambda_arn        = module.lambda.authorizer_lambda_arn
}

module "cloudfront" {
  source                               = "github.com/Yuki670926/rag-portfolio-modules//cloudfront?ref=v2.0.3"
  project_name                         = local.project_name
  frontend_bucket_id                   = module.s3.frontend_bucket_id
  frontend_bucket_arn                  = module.s3.frontend_bucket_arn
  frontend_bucket_regional_domain_name = module.s3.frontend_bucket_regional_domain_name
  web_acl_arn                          = module.waf.web_acl_arn
}

module "github_actions" {
  source               = "github.com/Yuki670926/rag-portfolio-modules//github_actions?ref=v1.2.1"
  project_name         = local.project_name
  github_username      = "Yuki670926"
  github_repo          = "rag-portfolio"
  frontend_bucket_name = module.s3.frontend_bucket_name
  cf_distribution_id   = module.cloudfront.distribution_id
}

module "presigned_url" {
  source                = "github.com/Yuki670926/rag-portfolio-modules//presigned_url?ref=v2.0.2"
  project_name          = local.project_name
  documents_bucket_name = module.s3.documents_bucket_name
  documents_bucket_arn  = module.s3.documents_bucket_arn
  rest_api_id           = module.api_gateway.rest_api_id
  root_resource_id      = module.api_gateway.root_resource_id
  authorizer_id         = module.api_gateway.authorizer_id
  execution_arn         = module.api_gateway.execution_arn
  lambda_authorizer_id  = module.api_gateway.lambda_authorizer_id
  cloudfront_domain     = module.cloudfront.distribution_domain_name # 追加
}

module "budgets" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//budgets?ref=v1.8.2"
  project_name = local.project_name
  environment  = var.environment
  budget_limit = "75"
}

module "cloudwatch" {
  source        = "github.com/Yuki670926/rag-portfolio-modules//cloudwatch?ref=v1.9.2"
  project_name  = local.project_name
  aws_region    = var.aws_region
  sns_topic_arn = try(module.eventbridge[0].sns_topic_arn, "")
}

module "dynamodb" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//dynamodb?ref=v2.0.1"
  project_name = local.project_name
  kms_key_arn  = module.kms.s3_kms_key_arn
}

module "ssm" {
  source                = "github.com/Yuki670926/rag-portfolio-modules//ssm?ref=v1.6.8"
  project_name          = local.project_name
  environment           = var.environment
  vector_store_endpoint = try(module.opensearch[0].collection_endpoint, "")
}

module "eventbridge" {
  count                    = var.opensearch_scheduled && var.vector_store_type == "opensearch" ? 1 : 0
  source                   = "github.com/Yuki670926/rag-portfolio-modules//eventbridge?ref=v1.9.3"
  project_name             = local.project_name
  environment              = var.environment
  aws_region               = var.aws_region
  collection_name          = "${local.project_name}-collection"
  ssm_endpoint_param       = "/rp/${var.environment}/vector-store/endpoint"
  pdf_indexes_table_name   = module.dynamodb.pdf_indexes_table_name
  pdf_indexes_table_arn    = module.dynamodb.pdf_indexes_table_arn
  ingest_lambda_arn        = module.lambda.ingest_lambda_arn
  ingest_lambda_name       = "${local.project_name}-ingest"
  documents_bucket_name    = module.s3.documents_bucket_name
  sns_topic_arn            = ""
  alert_email              = module.budgets.alert_email
  opensearch_start_dlq_arn = module.dlq_opensearch_start.dlq_arn
  opensearch_stop_dlq_arn  = module.dlq_opensearch_stop.dlq_arn
}
module "dlq_ingest" {
  source            = "github.com/Yuki670926/rag-portfolio-modules//dlq?ref=v2.0.1"
  project_name      = local.project_name
  environment       = var.environment
  queue_name_suffix = "ingest"
  kms_key_arn       = module.kms.sqs_kms_key_arn
}

module "dlq_opensearch_start" {
  source            = "github.com/Yuki670926/rag-portfolio-modules//dlq?ref=v2.0.1"
  project_name      = local.project_name
  environment       = var.environment
  queue_name_suffix = "opensearch-start"
  kms_key_arn       = module.kms.sqs_kms_key_arn
}

module "dlq_opensearch_stop" {
  source            = "github.com/Yuki670926/rag-portfolio-modules//dlq?ref=v2.0.1"
  project_name      = local.project_name
  environment       = var.environment
  queue_name_suffix = "opensearch-stop"
  kms_key_arn       = module.kms.sqs_kms_key_arn
}

module "kms" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//kms?ref=v2.0.0"
  project_name = local.project_name
  aws_region   = var.aws_region
  account_id   = var.account_id
}

module "waf" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//waf?ref=v2.0.3"
  project_name = local.project_name

  providers = {
    aws.us_east_1 = aws.us_east_1
  }
}
