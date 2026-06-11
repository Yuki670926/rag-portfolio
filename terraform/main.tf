terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.27"
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

locals {
  project_name = "rp-${var.environment}"
  # prod 強化の配線ポイント：データ保護（削除保護・prevent_destroy ガード・KMS 削除待機）は
  # prod のみ ON。dev/stag は検証で作り直す自由を残す（環境差分はこの 1 点に集約）
  is_prod = var.environment == "prod"
}

module "vpc" {
  source                    = "github.com/Yuki670926/rag-portfolio-modules//vpc?ref=v2.2.31"
  project_name              = local.project_name
  enable_private_networking = var.enable_private_networking
  # aoss-data EP は OpenSearch 使用時のみ（s3_vectors では不要な固定費のため作らない）
  aoss_endpoint_enabled = contains(["opensearch", "dual"], var.vector_store_type)
  # KB 系 EP / SSM EP も store 連動（層3 ON のとき「使う EP だけ」課金される）
  kb_endpoints_enabled = contains(["s3_vectors", "dual"], var.vector_store_type)
  ssm_endpoint_enabled = contains(["opensearch", "dual"], var.vector_store_type)
}

module "s3" {
  source            = "github.com/Yuki670926/rag-portfolio-modules//s3?ref=v2.2.33"
  project_name      = local.project_name
  account_id        = var.account_id
  ingest_lambda_arn = module.lambda.ingest_lambda_arn
  cloudfront_domain = module.cloudfront.distribution_domain_name
  kms_key_arn       = module.kms.s3_kms_key_arn
  api_url           = module.api_gateway.api_url
  user_pool_id      = module.cognito.user_pool_id
  client_id         = module.cognito.user_pool_client_id
  aws_region        = var.aws_region
  vector_store_type = var.vector_store_type
  # documents（正本）の誤破壊ガード：prod のみ prevent_destroy ガードを作成
  prevent_destroy = local.is_prod
}

module "cognito" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//cognito?ref=v2.2.34"
  project_name = local.project_name
  environment  = var.environment
  # admin のメール（＝ログイン username）はモジュール側が Secrets Manager
  # （rp-{env}-alert-email）から読む。公開リポにハードコードしない。
  aws_region = var.aws_region
  account_id = var.account_id
  # 登録ユーザー（再生成不能）の保護：prod のみ User Pool の置換 apply を明示エラーで停止
  deletion_protection = local.is_prod
}

module "lambda" {
  source                    = "github.com/Yuki670926/rag-portfolio-modules//lambda?ref=v2.2.31"
  project_name              = local.project_name
  documents_bucket_arn      = module.s3.documents_bucket_arn
  aws_region                = var.aws_region
  cognito_user_pool_id      = module.cognito.user_pool_id
  cognito_client_id         = module.cognito.user_pool_client_id
  conversations_table_name  = module.dynamodb.conversations_table_name
  sessions_table_name       = module.dynamodb.sessions_table_name
  vector_store_type         = var.vector_store_type
  environment               = var.environment
  ingest_dlq_arn            = module.dlq_ingest.dlq_arn
  subnet_ids                = module.vpc.private_subnet_ids
  lambda_security_group_id  = module.vpc.lambda_security_group_id
  knowledge_base_id         = try(module.knowledge_base[0].knowledge_base_id, "")
  data_source_id            = try(module.knowledge_base[0].data_source_id, "")
  knowledge_base_arn        = try(module.knowledge_base[0].knowledge_base_arn, "*")
  enable_private_networking = var.enable_private_networking
  kms_key_arn               = module.kms.s3_kms_key_arn
  pdf_indexes_table_name    = module.dynamodb.pdf_indexes_table_name
  pdf_indexes_table_arn     = module.dynamodb.pdf_indexes_table_arn
}

module "opensearch" {
  count                     = contains(["opensearch", "dual"], var.vector_store_type) ? 1 : 0
  source                    = "github.com/Yuki670926/rag-portfolio-modules//opensearch?ref=v2.2.34"
  project_name              = local.project_name
  ingest_lambda_role_arn    = module.lambda.ingest_lambda_role_arn
  query_lambda_role_arn     = module.lambda.query_lambda_role_arn
  enable_private_networking = var.enable_private_networking
  aoss_vpc_endpoint_id      = module.vpc.aoss_vpc_endpoint_id
  kms_key_arn               = module.kms.aoss_kms_key_arn
}

module "s3_vectors" {
  count        = contains(["s3_vectors", "dual"], var.vector_store_type) ? 1 : 0
  source       = "github.com/Yuki670926/rag-portfolio-modules//s3_vectors?ref=v2.2.1"
  project_name = local.project_name
  kms_key_arn  = module.kms.s3_kms_key_arn
}

module "knowledge_base" {
  count                = contains(["s3_vectors", "dual"], var.vector_store_type) ? 1 : 0
  source               = "github.com/Yuki670926/rag-portfolio-modules//knowledge_base?ref=v2.2.2"
  project_name         = local.project_name
  account_id           = var.account_id
  aws_region           = var.aws_region
  documents_bucket_arn = module.s3.documents_bucket_arn
  vector_bucket_arn    = module.s3_vectors[0].vector_bucket_arn
  vector_index_arn     = module.s3_vectors[0].vector_index_arn
  kms_key_arn          = module.kms.s3_kms_key_arn
}

module "api_gateway" {
  source                       = "github.com/Yuki670926/rag-portfolio-modules//api_gateway?ref=v2.2.34"
  project_name                 = local.project_name
  cognito_user_pool_arn        = module.cognito.user_pool_arn
  query_lambda_arn             = module.lambda.query_lambda_arn
  query_lambda_invoke_arn      = module.lambda.query_lambda_invoke_arn
  cloudfront_domain            = module.cloudfront.distribution_domain_name
  cognito_user_pool_id         = module.cognito.user_pool_id
  cognito_client_id            = module.cognito.user_pool_client_id
  authorizer_lambda_invoke_arn = module.lambda.authorizer_lambda_invoke_arn
  authorizer_lambda_arn        = module.lambda.authorizer_lambda_arn
  stage_name                   = var.environment
  # 別モジュール(presigned)のルート追加など、deployment スナップショットの取り直しが
  # 必要なときに bump する（履歴: /status 追加で 2→3→4）。
  deployment_revision = "4"
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
  source                      = "github.com/Yuki670926/rag-portfolio-modules//github_actions?ref=v2.2.25"
  project_name                = local.project_name
  environment                 = var.environment
  github_username             = "Yuki670926"
  github_repo                 = "rag-portfolio"
  frontend_bucket_arn         = module.s3.frontend_bucket_arn
  s3_kms_key_arn              = module.kms.s3_kms_key_arn
  cloudfront_distribution_arn = "arn:aws:cloudfront::${var.account_id}:distribution/${module.cloudfront.distribution_id}"
}

module "presigned_url" {
  source                 = "github.com/Yuki670926/rag-portfolio-modules//presigned_url?ref=v2.2.26"
  project_name           = local.project_name
  documents_bucket_name  = module.s3.documents_bucket_name
  documents_bucket_arn   = module.s3.documents_bucket_arn
  rest_api_id            = module.api_gateway.rest_api_id
  root_resource_id       = module.api_gateway.root_resource_id
  authorizer_id          = module.api_gateway.authorizer_id
  execution_arn          = module.api_gateway.execution_arn
  lambda_authorizer_id   = module.api_gateway.lambda_authorizer_id
  cloudfront_domain      = module.cloudfront.distribution_domain_name
  kms_key_arn            = module.kms.s3_kms_key_arn
  pdf_indexes_table_name = module.dynamodb.pdf_indexes_table_name
  pdf_indexes_table_arn  = module.dynamodb.pdf_indexes_table_arn
  vector_store_type      = var.vector_store_type
  knowledge_base_id      = try(module.knowledge_base[0].knowledge_base_id, "")
  data_source_id         = try(module.knowledge_base[0].data_source_id, "")
}

module "budgets" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//budgets?ref=v2.0.8"
  project_name = local.project_name
  environment  = var.environment
  budget_limit = "75"
}

module "cloudwatch" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//cloudwatch?ref=v2.2.34"
  project_name = local.project_name
  aws_region   = var.aws_region
  alert_email  = module.budgets.alert_email
  account_id   = var.account_id
  # AOSS の OCU 滞留アラーム（scale-to-zero 不全の検知）。GroupId は collection 再作成で
  # 変わるため output 経由で渡し自動追従。OpenSearch 不使用時は空＝アラーム自体を作らない
  aoss_collection_group_id   = try(module.opensearch[0].collection_group_id, "")
  aoss_collection_group_name = try(module.opensearch[0].collection_group_name, "")
}

module "dynamodb" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//dynamodb?ref=v2.2.33"
  project_name = local.project_name
  kms_key_arn  = module.kms.s3_kms_key_arn
  # 会話履歴等（正本）の保護：prod のみ DeleteTable を API レベルで拒否
  deletion_protection = local.is_prod
}

module "ssm" {
  source                = "github.com/Yuki670926/rag-portfolio-modules//ssm?ref=v2.2.31"
  project_name          = local.project_name
  environment           = var.environment
  vector_store_endpoint = try(module.opensearch[0].collection_endpoint, "")
  vector_store_type     = var.vector_store_type
}

module "dlq_ingest" {
  source            = "github.com/Yuki670926/rag-portfolio-modules//dlq?ref=v2.0.1"
  project_name      = local.project_name
  environment       = var.environment
  queue_name_suffix = "ingest"
  kms_key_arn       = module.kms.sqs_kms_key_arn
}



module "kms" {
  source = "github.com/Yuki670926/rag-portfolio-modules//kms?ref=v2.2.34"
  # aoss 用 CMK は OpenSearch 使用時のみ作成（全データストア CMK 統一・s3_vectors では不要な$1/月を回避）
  create_aoss_key = contains(["opensearch", "dual"], var.vector_store_type)
  project_name    = local.project_name
  aws_region      = var.aws_region
  account_id      = var.account_id
  # 正本を抱える s3 鍵の削除待機（取消猶予）：prod は 30 日・dev/stag は最短 7 日
  s3_key_deletion_window_in_days = local.is_prod ? 30 : 7
  # aoss 鍵の grant 経路を CI ロールに明示許可（AdministratorAccess への暗黙依存を解消。
  # ロール ARN は循環回避のため構築文字列で参照＝github_actions モジュールに依存を張らない）
  aoss_grant_principal_arns = ["arn:aws:iam::${var.account_id}:role/${local.project_name}-github-actions-role"]
}

module "waf" {
  source       = "github.com/Yuki670926/rag-portfolio-modules//waf?ref=v2.0.3"
  project_name = local.project_name

  providers = {
    aws.us_east_1 = aws.us_east_1
  }
}

# authorizer 用 IAM ロール／ポリシーの同名二重定義の解消（api_gateway v2.2.34 で定義を撤去）。
# 物理ロールは lambda モジュールの同名アドレスが管理を継続するため、api_gateway 側の
# アドレスは destroy せず state から外すだけにする（destroy すると共有物理ロールが
# 消えて authorizer が停止する）。
removed {
  from = module.api_gateway.aws_iam_role.lambda_authorizer

  lifecycle {
    destroy = false
  }
}

removed {
  from = module.api_gateway.aws_iam_role_policy.lambda_authorizer

  lifecycle {
    destroy = false
  }
}

# 【移行中】per-env trail は組織 trail と記録範囲が100%重複し、管理イベントの
# 2コピー目として課金されるため組織 trail に一本化する（実測: dev だけで月換算 ~$5）。
# force_destroy=true は次のリリースでモジュールごと撤去するための準備
# （同一イベントは組織 trail に記録済みのため監査ログは失われない）。
module "cloudtrail" {
  source        = "github.com/Yuki670926/rag-portfolio-modules//cloudtrail?ref=v2.2.34"
  project_name  = local.project_name
  account_id    = var.account_id
  aws_region    = var.aws_region
  kms_key_arn   = module.kms.cloudtrail_kms_key_arn
  force_destroy = true
}
