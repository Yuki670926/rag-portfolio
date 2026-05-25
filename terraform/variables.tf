variable "environment" {
  type        = string
  description = "環境名（dev / stag / prod）"
}

variable "aws_region" {
  type        = string
  description = "AWSリージョン"
  default     = "ap-northeast-1"
}

variable "account_id" {
  type        = string
  description = "AWSアカウントID"
}

variable "vector_store_type" {
  type        = string
  description = "ベクトルストアの種類（opensearch or s3_vectors）"
  default     = "opensearch"
}