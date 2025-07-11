## Reporting Security Issues

Amazon Web Services (AWS) is dedicated to the responsible disclosure of security vulnerabilities.  
  
We kindly ask that you **do not** open a public GitHub issue to report security concerns.  
  
Instead, please submit the issue to the AWS Vulnerability Disclosure Program via [HackerOne](https://hackerone.com/aws_vdp) or send your report via [email](mailto:aws-security@amazon.com).  
  
For more details, visit the [AWS Vulnerability Reporting Page](http://aws.amazon.com/security/vulnerability-reporting/).  

Thank you in advance for collaborating with us to help protect our customers.

## OpenPGP Keys for AWS Deadline Cloud
### Current Keys
Select the "Copy" icon to copy the following key:
```
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGX6GQsBEADduUtJgqSXI+q76O6fsFwEYKmbnlyL0xKvlq32EZuyv0otZo5L
le4m5Gg52AzrvPvDiUTLooAlvYeozaYyirIGsK08Ydz0Ftdjroiuh/mw9JSJDJRI
rnRn5yKet1JFezkjopA3pjsTBP6lW/mb1bDBDEwwwtH0x9lV7A03FJ9T7Uzu/qSh
qO/UYdkafro3cPASvkqgDt2tCvURfBcUCAjZVFcLZcVD5iwXacxvKsxxS/e7kuVV
I1+VGT8Hj8XzWYhjCZxOLZk/fvpYPMyEEujN0fYUp6RtMIXve0C9awwMCy5nBG2J
eE2Ol5DsCpTaBd4Fdr3LWcSs8JFA/YfP9auL3NczOozPoVJt+fw8CBlVIXO0J7l5
hvHDjcC+5v0wxqAlMG6+f/SX7CT8FXK+L3iOJ5gBYUNXqHSxUdv8kt76/KVmQa1B
Akl+MPKpMq+lhw++S3G/lXqwWaDNQbRRw7dSZHymQVXvPp1nsqc3hV7KlOM+6s6g
1g4mvFY4lf6DhptwZLWyQXU8rBQpojvQfiSmDFrFPWFi5BexesuVnkGIolQoklKx
AVUSdJPVEJCteyy7td4FPhBaSqT5vW3+ANbr9b/uoRYWJvn17dN0cc9HuRh/Ai+I
nkfECo2WUDLZ0fEKGjGyFX+todWvJXjvc5kmE9Ty5vJp+M9Vvb8jd6t+mwARAQAB
tCxBV1MgRGVhZGxpbmUgQ2xvdWQgPGF3cy1kZWFkbGluZUBhbWF6b24uY29tPokC
VwQTAQgAQRYhBLhAwIwpqQeWoHH6pfbNPOa3bzzvBQJl+hkLAxsvBAUJA8JnAAUL
CQgHAgIiAgYVCgkICwIDFgIBAh4HAheAAAoJEPbNPOa3bzzvKswQAJXzKSAY8sY8
F6Eas2oYwIDDdDurs8FiEnFghjUEO6MTt9AykF/jw+CQg2UzFtEyObHBymhgmhXE
3buVeom96tgM3ZDfZu+sxi5pGX6oAQnZ6riztN+VpkpQmLgwtMGpSMLl3KLwnv2k
WK8mrR/fPMkfdaewB7A6RIUYiW33GAL4KfMIs8/vIwIJw99NxHpZQVoU6dFpuDtE
1OuxGcCqGJ7mAmo6H/YawSNp2Ns80gyqIKYo7o3LJ+WRroIRlQyctq8gnR9JvYXX
42ASqLq5+OXKo4qh81blXKYqtc176BbbSNFjWnzIQgKDgNiHFZCdcOVgqDhwO15r
NICbqqwwNLj/Fr2kecYx180Ktpl0jOOw5IOyh3bf3MVGWnYRdjvA1v+/CO+55N4g
z0kf50Lcdu5RtqV10XBCifn28pecqPaSdYcssYSRl5DLiFktGbNzTGcZZwITTKQc
af8PPdTGtnnb6P+cdbW3bt9MVtN5/dgSHLThnS8MPEuNCtkTnpXshuVuBGgwBMdb
qUC+HjqvhZzbwns8dr5WI+6HWNBFgGANn6ageYl58vVp0UkuNP8wcWjRARciHXZx
ku6W2jPTHDWGNrBQO2Fx7fd2QYJheIPPAShHcfJO+xgWCof45D0vAxAJ8gGg9Eq+
gFWhsx4NSHn2gh1gDZ41Ou/4exJ1lwPM
=uVaX
-----END PGP PUBLIC KEY BLOCK-----
```

## Verifying GitHub Releases

You can verify the authenticity of the release artifacts using the `gpg` command line tool.

1) Download the desired release artifacts from the GitHub releases page. Make sure to download the corresponding PGP signature file (ending with `.sig`) as well.
For example, if you would like to verify your download of the wheel for version `x.x.x`, you should have the following files downloaded:
    ```
    <package>-x.x.x-py3-none-any.whl
    <package>-x.x.x-py3-none-any.whl.sig
    ```

2) Install the `gpg` command line tool. The installation process varies by operating system. Please refer to the GnuPG website for instructions: https://gnupg.org/download/

3) Save the OpenPGP key from the [OpenPGP Keys for AWS Deadline Cloud](#openpgp-keys-for-aws-deadline-cloud) section above to a file called `awsdeadlinecloud-pgp.asc`.


4) Import the OpenPGP key for AWS Deadline Cloud by running the following command:

    ```
    gpg --import --armor awsdeadlinecloud-pgp.asc
    ```

    Response:
    ```
    gpg: key ################: public key "AWS Deadline Cloud <aws-deadline@amazon.com>" imported
    ```

5) Determine whether to trust the OpenPGP key. Some factors to consider when deciding whether or not to trust the above key are:

    - The internet connection you’ve used to obtain the GPG key from this website is secure
    - The device that you are accessing this website on is secure

    If you have decided to trust the OpenPGP key, then edit the key to trust with `gpg` like the following example:
    ```
    $ gpg --edit-key **<replace with 16 character key from step 4>**
    gpg (GnuPG) 2.0.22; Copyright (C) 2013 Free Software Foundation, Inc.
    This is free software: you are free to change and redistribute it.
    There is NO WARRANTY, to the extent permitted by law.


    pub  4096R/4BF0B8D2  created: 2023-06-23  expires: 2025-06-22  usage: SCEA
                         trust: unknown       validity: unknown
    [ unknown] (1). AWS Deadline Cloud example@example.com

    gpg> trust
    pub  4096R/4BF0B8D2  created: 2023-06-23  expires: 2025-06-22  usage: SCEA
                         trust: unknown       validity: unknown
    [ unknown] (1). AWS Deadline Cloud aws-deadline@amazon.com

    Please decide how far you trust this user to correctly verify other users' keys
    (by looking at passports, checking fingerprints from different sources, etc.)

      1 = I don't know or won't say
      2 = I do NOT trust
      3 = I trust marginally
      4 = I trust fully
      5 = I trust ultimately
      m = back to the main menu

    Your decision? 5
    Do you really want to set this key to ultimate trust? (y/N) y

    pub  4096R/4BF0B8D2  created: 2023-06-23  expires: 2025-06-22  usage: SCEA
                         trust: ultimate      validity: unknown
    [ unknown] (1). AWS Deadline Cloud aws-deadline@amazon.com
    Please note that the shown key validity is not necessarily correct
    unless you restart the program.

    gpg> quit
    ```

6) Verify the signature of the an AWS Deadline Cloud release via `gpg --verify`. The command for verifying the example files from step 1 would be:

    ```
    gpg --verify ./<package>-x.x.x-py3-none-any.whl.sig ./<package>-x.x.x-py3-none-any.whl
    ```