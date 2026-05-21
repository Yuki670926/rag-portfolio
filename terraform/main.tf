terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
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
  source            = "./modules/s3"
  project_name      = "rag-portfolio"
  account_id        = "086769945521"
  ingest_lambda_arn = module.lambda.ingest_lambda_arn
}

module "cognito" {
  source       = "./modules/cognito"
  project_name = "rag-portfolio"
}

module "lambda" {
  source               = "./modules/lambda"
  project_name         = "rag-portfolio"
  documents_bucket_arn = module.s3.documents_bucket_arn
  opensearch_endpoint  = module.opensearch.collection_endpoint
}

module "opensearch" {
  source          = "./modules/opensearch"
  project_name    = "rag-portfolio"
  lambda_role_arn = module.lambda.lambda_role_arn
}

module "api_gateway" {
  source                  = "./modules/api_gateway"
  project_name            = "rag-portfolio"
  cognito_user_pool_arn   = module.cognito.user_pool_arn
  query_lambda_arn        = module.lambda.query_lambda_arn
  query_lambda_invoke_arn = module.lambda.query_lambda_invoke_arn
}

module "cloudfront" {
  source                               = "./modules/cloudfront"
  project_name                         = "rag-portfolio"
  frontend_bucket_id                   = module.s3.frontend_bucket_id
  frontend_bucket_arn                  = module.s3.frontend_bucket_arn
  frontend_bucket_regional_domain_name = module.s3.frontend_bucket_regional_domain_name
}

module "github_actions" {
  source          = "./modules/github_actions"
  project_name    = "rag-portfolio"
  github_username = "Yuki670926"
  github_repo     = "rag-portfolio"
}

module "presigned_url" {
  source                = "./modules/presigned_url"
  project_name          = "rag-portfolio"
  lambda_role_arn       = module.lambda.lambda_role_arn
  documents_bucket_name = module.s3.documents_bucket_name
  rest_api_id           = module.api_gateway.rest_api_id
  root_resource_id      = module.api_gateway.root_resource_id
  authorizer_id         = module.api_gateway.authorizer_id
  execution_arn         = module.api_gateway.execution_arn
}